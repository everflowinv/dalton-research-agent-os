"""Rebuildable per-company research view over existing Core authorities.

A pure projection: reads the Ledger snapshot, thesis admissions, the research
question backlog, thesis-impact records and published weekly briefs through
one Core connection and returns a closed, self-hashing record.  Nothing is
written, no second fact authority exists, and the same authority state always
rebuilds a byte-identical view — ``built_as_of`` is the newest authority
timestamp among the inputs, never a wall clock.  Every row carries immutable
refs and hashes so callers can re-read the exact versions from the Ledger and
hand them to the ContextMaterializer inside a token-bounded ContextPack.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .store import DaltonStore, canonical_json, content_hash
from .forecast_reconciliation import validate_forecast_reconciliation


SCHEMA_VERSION = "0.1"
PROJECTION_KIND = "company_research_view"
CLAIM_STATUSES = frozenset({
    "proposed", "corroborated", "contested", "superseded", "retracted",
})
OPEN_QUESTION_STATES = frozenset({
    "open", "selected", "planned", "in_progress", "blocked",
})
# Where an untagged claim sorts among tagged ones: after all of them. Weaker
# than any importance tier, undated, and last on every tiebreak.
_UNTAGGED_ORDER = (99, (1, ""), "", "")
STOP_KINDS = frozenset({
    "claim_committed", "thesis_admitted", "assessment_recorded",
    "verification_recorded", "brief_published", "question_recorded",
})


class CompanyResearchViewError(RuntimeError):
    """Base error for the company research view projection."""


class CompanyResearchViewValidationError(CompanyResearchViewError):
    """A request does not satisfy the closed contract."""


@dataclass(frozen=True)
class CompanyClaimQueryContext:
    """One operation's immutable projection of one company's Claim snapshot."""

    connection: Any
    company_ref: str
    rows: tuple[Mapping[str, Any], ...]


def prepare_company_claim_query(
    store: DaltonStore, company_ref: str,
) -> CompanyClaimQueryContext:
    """Read ClaimIndex once for repeated queries within one operation.

    The returned value is deliberately not cached.  Its connection and company
    binding prevent a caller from carrying a projection into another authority
    or company operation.
    """

    company_ref = _text(company_ref, "company_ref")
    snapshot = store.claim_index_snapshot()
    return CompanyClaimQueryContext(
        connection=store.connection,
        company_ref=company_ref,
        rows=tuple(_claim_rows(store, snapshot, company_ref)),
    )


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CompanyResearchViewValidationError(f"{name} must be non-empty text")
    return value.strip()


def _period_key(period: Any) -> str:
    if isinstance(period, str):
        return period
    return canonical_json(period)


def _table_exists(connection: Any, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _covers_company(issue: Mapping[str, Any], company_ref: str) -> bool:
    for binding in issue.get("thesis_bindings") or []:
        if binding.get("company_ref") == company_ref:
            return True
    for overlay in issue.get("company_overlay_versions") or []:
        if overlay.get("company_ref") == company_ref:
            return True
    return False


def _claim_rows(
    store: DaltonStore,
    snapshot: Mapping[str, Any],
    company_ref: str | None,
) -> list[dict[str, Any]]:
    """Project the latest ClaimVersion per claim_ref with derived status."""

    latest: dict[str, str] = dict(snapshot.get("latest_claim_version_refs") or {})
    versions: dict[str, dict[str, Any]] = {
        row["claim_version_id"]: row
        for row in snapshot.get("claim_versions") or []
    }
    connection = store.connection
    selected: list[tuple[str, str, dict[str, Any]]] = []
    for claim_ref, version_ref in sorted(latest.items()):
        row = versions.get(version_ref)
        if row is None:
            raise CompanyResearchViewError("ledger snapshot lost a claim version")
        claim = row["claim"]
        if company_ref is not None and claim.get("subject_ref") != company_ref:
            continue
        selected.append((claim_ref, version_ref, row))

    # Fetch evidence metadata in bounded batches. The former query lived in
    # the loop below, so rebuilding one company scanned the whole unindexed
    # evidence_relations table once per ClaimVersion (an N+1 full scan).
    # Source types are a set and retrieved_at is a maximum, so row order has
    # never contributed to the projection's bytes.
    evidence_by_version: dict[str, list[dict[str, Any]]] = {
        version_ref: [] for _, version_ref, _ in selected
    }
    refs = list(evidence_by_version)
    for offset in range(0, len(refs), 400):
        batch = refs[offset:offset + 400]
        placeholders = ",".join("?" for _ in batch)
        for evidence_row in connection.execute(
            "SELECT r.claim_version_id,e.evidence_json FROM evidence_relations r "
            "JOIN evidence_versions e ON e.evidence_version_id=r.evidence_version_id "
            f"WHERE r.claim_version_id IN ({placeholders})", batch,
        ).fetchall():
            evidence_by_version[evidence_row["claim_version_id"]].append(
                json.loads(evidence_row["evidence_json"]))

    rows: list[dict[str, Any]] = []
    for claim_ref, version_ref, row in selected:
        claim = row["claim"]
        status = DaltonStore.project_claim_status(snapshot, version_ref)
        retrieved: list[str] = []
        source_types: list[str] = []
        for evidence in evidence_by_version[version_ref]:
            if isinstance(evidence.get("retrieved_at"), str):
                retrieved.append(evidence["retrieved_at"])
            if isinstance(evidence.get("source_type"), str):
                source_types.append(evidence["source_type"])
        rows.append({
            "claim_ref": claim_ref,
            "claim_version_ref": version_ref,
            "claim_version_hash": row["content_hash"],
            "subject_ref": claim.get("subject_ref"),
            "metric_or_aspect": claim.get("metric_or_aspect"),
            "period": claim.get("period"),
            "period_key": _period_key(claim.get("period")),
            "claim_kind": claim.get("claim_kind"),
            "value": claim.get("value"),
            "unit": claim.get("unit"),
            "normalized_statement": claim.get("normalized_statement"),
            "status": status,
            "latest_evidence_retrieved_at": max(retrieved) if retrieved else None,
            "source_types": sorted(set(source_types)),
            "created_at": row.get("created_at"),
        })
    return rows


def _thesis_id_for(connection: Any, company_ref: str) -> str | None:
    row = connection.execute(
        "SELECT v.thesis_id FROM thesis_admission_candidates c "
        "JOIN thesis_admission_decisions d ON d.candidate_id=c.candidate_id "
        "JOIN thesis_versions v ON v.admission_decision_id=d.decision_id "
        "JOIN current_pointers p ON p.version_id=v.version_id "
        "WHERE c.company_ref=? AND v.version_number=1 "
        "ORDER BY p.updated_at DESC LIMIT 1",
        (company_ref,),
    ).fetchone()
    return None if row is None else row["thesis_id"]


def build_company_research_view(
    store: DaltonStore, company_ref: str
) -> dict[str, Any]:
    """Build the closed, deterministic research view for one company."""

    company_ref = _text(company_ref, "company_ref")
    connection = store.connection
    snapshot = store.claim_index_snapshot()

    claims = _claim_rows(store, snapshot, company_ref)

    thesis_id = _thesis_id_for(connection, company_ref)
    thesis: dict[str, Any] = {
        "status": "insufficient",
        "thesis_ref": None,
        "thesis_version_ref": None,
        "thesis_version_hash": None,
        "statement": None,
        "confidence": None,
        "template_ref": None,
        "driver_refs": [],
        "falsifier_refs": [],
        "claim_refs": [],
        "industry_ref": None,
    }
    thesis_version_created: str | None = None
    if thesis_id is not None:
        pointer = connection.execute(
            "SELECT * FROM current_pointers WHERE thesis_id=?", (thesis_id,)
        ).fetchone()
        if pointer is not None:
            version = connection.execute(
                "SELECT * FROM thesis_versions WHERE version_id=?",
                (pointer["version_id"],),
            ).fetchone()
            candidate = connection.execute(
                "SELECT c.record_json AS candidate_json FROM thesis_versions v "
                "JOIN thesis_admission_decisions d ON d.decision_id=v.admission_decision_id "
                "JOIN thesis_admission_candidates c ON c.candidate_id=d.candidate_id "
                "WHERE v.version_id=? AND v.version_number=1",
                (pointer["version_id"],),
            ).fetchone()
            content = json.loads(version["content_json"])
            thesis_version_created = version["created_at"]
            if content.get("content_hash") == pointer["content_hash"]:
                thesis = {
                    "status": "current",
                    "thesis_ref": thesis_id,
                    "thesis_version_ref": pointer["version_id"],
                    "thesis_version_hash": pointer["content_hash"],
                    "statement": content.get("statement"),
                    "confidence": content.get("confidence"),
                    "template_ref": (
                        json.loads(candidate["candidate_json"]).get("template_ref")
                        if candidate is not None else None
                    ),
                    "driver_refs": (
                        list(json.loads(candidate["candidate_json"]).get("driver_refs") or [])
                        if candidate is not None else []
                    ),
                    "falsifier_refs": list(content.get("falsifier_refs") or []),
                    "claim_refs": list(content.get("claim_refs") or []),
                    "industry_ref": (
                        json.loads(candidate["candidate_json"]).get("industry_ref")
                        if candidate is not None else None
                    ),
                }

    open_questions: list[dict[str, Any]] = []
    if _table_exists(connection, "backlog_question_versions"):
        question_rows = connection.execute(
            "SELECT q.question_ref, v.record_json AS record_json, "
            "(SELECT e.state FROM backlog_question_events e "
            " WHERE e.question_ref=q.question_ref ORDER BY e.created_at DESC, e.rowid DESC LIMIT 1"
            ") AS state "
            "FROM backlog_questions q JOIN backlog_question_pointer p "
            "ON p.question_ref=q.question_ref "
            "JOIN backlog_question_versions v ON v.version_id=p.version_id "
            "WHERE v.company_ref=? ORDER BY q.question_ref",
            (company_ref,),
        ).fetchall()
    else:
        question_rows = []
    question_created: list[str] = []
    for row in question_rows:
        record = json.loads(row["record_json"])
        state = row["state"] or "open"
        if record.get("created_at"):
            question_created.append(record["created_at"])
        if state not in OPEN_QUESTION_STATES:
            continue
        open_questions.append({
            "question_ref": row["question_ref"],
            "state": state,
            "question": record.get("question"),
            "mandate_ref": record.get("mandate_ref"),
            "created_at": record.get("created_at"),
        })

    claim_refs = {row["claim_version_ref"] for row in claims}
    impact: list[dict[str, Any]] = []
    impact_rows: list[Any] = []
    if _table_exists(connection, "thesis_impact_assessments"):
        impact_rows = connection.execute(
        "SELECT a.assessment_id, a.impact, a.claim_version_ref, a.created_at, "
        "v.verdict AS verification_verdict, v.verification_id "
        "FROM thesis_impact_assessments a "
        "LEFT JOIN thesis_impact_verifications v ON v.assessment_ref=a.assessment_id "
        "WHERE a.claim_version_ref IN (SELECT claim_version_id FROM claim_versions "
        "WHERE json_extract(claim_json,'$.subject_ref')=?) "
        "ORDER BY a.created_at, a.assessment_id",
        (company_ref,),
    ).fetchall()
    for row in impact_rows:
        impact.append({
            "assessment_ref": row["assessment_id"],
            "claim_version_ref": row["claim_version_ref"],
            "impact": row["impact"],
            "verification_ref": row["verification_id"],
            "verdict": row["verification_verdict"],
            "created_at": row["created_at"],
        })

    reconciliations: list[dict[str, Any]] = []
    if _table_exists(connection, "forecast_reconciliations"):
        for row in connection.execute(
            "SELECT record_json, content_hash FROM forecast_reconciliations "
            "WHERE subject_ref=? ORDER BY created_at, reconciliation_id",
            (company_ref,),
        ).fetchall():
            record = validate_forecast_reconciliation(json.loads(row["record_json"]))
            if record["content_hash"] != row["content_hash"]:
                raise CompanyResearchViewError("forecast reconciliation row hash drifted")
            decided = connection.execute(
                "SELECT decision FROM forecast_overturn_decisions WHERE reconciliation_ref=?",
                (record["id"],),
            ).fetchone()
            reconciliations.append({
                "reconciliation_ref": record["id"],
                "reconciliation_hash": record["content_hash"],
                "metric_ref": record["metric_ref"],
                "period": f"{record['period']['start']}..{record['period']['end']}",
                "forecast_line_version_ref": record["forecast_line_version_ref"],
                "claim_version_ref": record["claim_version_ref"],
                "forecast_value": record["forecast_value"],
                "actual_value": record["actual_value"],
                "unit": record["unit"],
                "currency": record["currency"],
                "deviation_percent": record["deviation_percent"],
                "direction": record["direction"],
                "band": record["band"],
                "human_checkpoint": record["human_checkpoint"],
                "checkpoint_status": (
                    "not_required" if record["human_checkpoint"] is None
                    else ("pending_human" if decided is None else f"decided:{decided['decision']}")
                ),
                "created_at": record["created_at"],
            })

    issues: list[Any] = []
    if _table_exists(connection, "weekly_brief_issue_versions"):
        issues = connection.execute(
            "SELECT version_id, brief_ref, version_number, record_json, content_hash, "
            "created_at FROM weekly_brief_issue_versions ORDER BY version_number"
        ).fetchall()
    last_issue: dict[str, Any] | None = None
    for row in issues:
        record = json.loads(row["record_json"])
        if _covers_company(record, company_ref):
            last_issue = {
                "issue_version_ref": row["version_id"],
                "issue_version_hash": row["content_hash"],
                "brief_ref": row["brief_ref"],
                "period": record.get("period"),
                "created_at": row["created_at"],
            }

    stops: list[dict[str, Any]] = []
    for row in claims:
        stops.append({
            "kind": "claim_committed", "ref": row["claim_version_ref"],
            "at": row["created_at"],
        })
    if thesis_version_created is not None and thesis["status"] == "current":
        stops.append({
            "kind": "thesis_admitted", "ref": thesis["thesis_version_ref"],
            "at": thesis_version_created,
        })
    for row in impact:
        stops.append({
            "kind": "assessment_recorded", "ref": row["assessment_ref"],
            "at": row["created_at"],
        })
        if row["verification_ref"] is not None and row["created_at"]:
            stops.append({
                "kind": "verification_recorded", "ref": row["verification_ref"],
                "at": row["created_at"],
            })
    for row in open_questions:
        stops.append({
            "kind": "question_recorded", "ref": row["question_ref"],
            "at": row["created_at"],
        })
    for row in reconciliations:
        stops.append({
            "kind": "forecast_reconciled", "ref": row["reconciliation_ref"],
            "at": row["created_at"],
        })
    if last_issue is not None:
        stops.append({
            "kind": "brief_published", "ref": last_issue["issue_version_ref"],
            "at": last_issue["created_at"],
        })
    stops = [item for item in stops if item["at"]]
    stops.sort(key=lambda item: (item["at"], item["kind"], item["ref"]))
    last_stop = stops[-1] if stops else None

    timestamps = [item["at"] for item in stops]
    for row in claims:
        if row["created_at"]:
            timestamps.append(row["created_at"])
    for row in question_rows:
        record = json.loads(row["record_json"])
        if record.get("created_at"):
            timestamps.append(record["created_at"])
    for row in impact_rows:
        timestamps.append(row["created_at"])
    built_as_of = max(timestamps) if timestamps else None

    body = {
        "schema_version": SCHEMA_VERSION,
        "projection_kind": PROJECTION_KIND,
        "company_ref": company_ref,
        "built_as_of": built_as_of,
        "thesis": thesis,
        "claims": claims,
        "open_questions": open_questions,
        "impact": impact,
        "forecast_reconciliations": reconciliations,
        "last_weekly_issue": last_issue,
        "last_research_stop": last_stop,
    }
    view = dict(body)
    view["id"] = f"company-research-view:{content_hash(body)[:32]}"
    view["content_hash"] = content_hash({
        key: value for key, value in view.items() if key != "content_hash"
    })
    return view


def annotate_with_index(
    connection: Any,
    rows: Sequence[Mapping[str, Any]],
    *,
    ref_key: str = "claim_version_ref",
    index_aspect: str | None = None,
    as_of_from: str | None = None,
    as_of_to: str | None = None,
    importance: str | None = None,
    canonical_only: bool = True,
) -> list[dict[str, Any]]:
    """Join P12b index entries onto claim rows and filter by them.

    Separate from ``query_company_research`` because more than one reader wants
    it: the cockpit's answer context reads ``claim_versions`` directly and
    needs the same canonical-only, importance-ordered view without going
    through the projection.

    A Core with no index answers exactly as it did before P12b: every row is
    returned, every index field is ``None``, and every index filter is
    inapplicable rather than silently empty.  A filter that *needs* the index
    is refused on an unindexed Core rather than quietly matching nothing --
    "there is no index here" and "nothing matched" are different answers and a
    caller that cannot tell them apart will draw the wrong conclusion.
    """

    from .claim_index_authority import (
        IMPORTANCE_TIERS, canonical_order_key, current_entries, table_exists,
    )
    from .claim_aspect_vocabulary import ASPECTS

    if index_aspect is not None and index_aspect not in ASPECTS:
        raise CompanyResearchViewValidationError(
            "index_aspect must be one of " + ", ".join(ASPECTS)
        )
    if importance is not None and importance not in IMPORTANCE_TIERS:
        raise CompanyResearchViewValidationError(
            "importance must be one of " + ", ".join(IMPORTANCE_TIERS)
        )
    for name, value in (("as_of_from", as_of_from), ("as_of_to", as_of_to)):
        if value is not None:
            _text(value, name)
    requires_index = any(
        value is not None
        for value in (index_aspect, as_of_from, as_of_to, importance)
    )
    indexed = table_exists(connection)
    if requires_index and not indexed:
        raise CompanyResearchViewValidationError(
            "this Core holds no Claim index; aspect, as_of and importance "
            "filters cannot be answered here"
        )
    entries = (
        current_entries(connection, claim_version_refs=[row[ref_key] for row in rows])
        if indexed else {}
    )
    result: list[dict[str, Any]] = []
    for row in rows:
        entry = entries.get(row[ref_key])
        if entry is None:
            # An index that has not reached this claim yet cannot say whether
            # it is a duplicate, so ``canonical_only`` does not drop it: an
            # untagged claim is a gap in the index, never a reason to hide a
            # fact.  An explicit aspect/date/importance filter does drop it,
            # because the answer to "which claims are tagged X" cannot include
            # one that is tagged nothing.
            if requires_index:
                continue
            result.append({
                **dict(row),
                "index_aspect": None, "as_of": None, "as_of_basis": None,
                "importance": None, "dedupe_group_ref": None,
                "is_canonical": None, "index_entry_ref": None,
                # Present on every row or on none: a sort key that exists only
                # for the rows that happen to be tagged cannot sort a list.
                # An untagged claim sorts after every tagged one.
                "index_order": _UNTAGGED_ORDER,
            })
            continue
        if index_aspect is not None and entry["aspect"] != index_aspect:
            continue
        if importance is not None and entry["importance"] != importance:
            continue
        if as_of_from is not None and (entry["as_of"] is None or entry["as_of"] < as_of_from):
            continue
        if as_of_to is not None and (entry["as_of"] is None or entry["as_of"] > as_of_to):
            continue
        if canonical_only and not entry["is_canonical"]:
            continue
        result.append({
            **dict(row),
            "index_aspect": entry["aspect"],
            "as_of": entry["as_of"],
            "as_of_basis": entry["as_of_basis"],
            "importance": entry["importance"],
            "dedupe_group_ref": entry["dedupe_group_ref"],
            "is_canonical": entry["is_canonical"],
            "index_entry_ref": entry["id"],
            "index_order": canonical_order_key(entry),
        })
    return result


def query_company_research(
    store: DaltonStore,
    *,
    company_ref: str | None = None,
    aspect: str | None = None,
    period: str | None = None,
    status: str | None = None,
    limit: int = 100,
    index_aspect: str | None = None,
    as_of_from: str | None = None,
    as_of_to: str | None = None,
    importance: str | None = None,
    canonical_only: bool = True,
    exclude_retired: bool = False,
    claim_context: CompanyClaimQueryContext | None = None,
) -> list[dict[str, Any]]:
    """Structured query over claim rows; returns immutable refs and hashes.

    ``aspect`` still filters the Ledger's free-text ``metric_or_aspect``; the
    P12b closed vocabulary is ``index_aspect``.  Two names because they are two
    fields and always were -- collapsing them would silently change what every
    existing caller of ``aspect`` asks for.

    ``canonical_only`` defaults to true, which is the whole point of the index:
    a reader asking for a company's claims should get one copy of each fact,
    not the same quarter's revenue three times.  It only ever removes a row
    that the index has positively marked as a duplicate of another; a claim
    with no index entry, and every claim in a Core with no index at all, is
    returned exactly as before.

    ``exclude_retired`` is opt-in so existing projections retain their exact
    historical behavior. When enabled, retirement is applied before ``limit``.
    """

    if company_ref is not None:
        company_ref = _text(company_ref, "company_ref")
    if aspect is not None:
        aspect = _text(aspect, "aspect")
    if period is not None:
        period = _text(period, "period")
    if status is not None and status not in CLAIM_STATUSES:
        raise CompanyResearchViewValidationError(
            "status must be one of " + ", ".join(sorted(CLAIM_STATUSES))
        )
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise CompanyResearchViewValidationError("limit must be between 1 and 1000")
    if claim_context is None:
        snapshot = store.claim_index_snapshot()
        rows = _claim_rows(store, snapshot, company_ref)
    else:
        if company_ref is None:
            raise CompanyResearchViewValidationError(
                "claim_context requires an exact company_ref")
        if claim_context.connection is not store.connection:
            raise CompanyResearchViewValidationError(
                "claim_context belongs to a different authority connection")
        if claim_context.company_ref != company_ref:
            raise CompanyResearchViewValidationError(
                "claim_context belongs to a different company")
        rows = claim_context.rows
    filtered = []
    for row in rows:
        if aspect is not None and row["metric_or_aspect"] != aspect:
            continue
        if period is not None and row["period_key"] != period:
            continue
        if status is not None and row["status"] != status:
            continue
        filtered.append({
            key: value for key, value in row.items() if key != "period_key"
        })
    from .claim_index_authority import table_exists

    joined = annotate_with_index(
        store.connection, filtered, index_aspect=index_aspect,
        as_of_from=as_of_from, as_of_to=as_of_to, importance=importance,
        canonical_only=canonical_only,
    )
    retirement_table = None
    if exclude_retired:
        retirement_table = store.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            ("claim_retirement_decisions",),
        ).fetchone()
    if retirement_table is not None:
        retired = {
            str(row[0]) for row in store.connection.execute(
                "SELECT claim_version_ref FROM claim_retirement_decisions "
                "WHERE decision='retired'"
            ).fetchall()
        }
        joined = [row for row in joined
                  if str(row["claim_version_ref"]) not in retired]
    # A Core that has never opened the index answers byte-identically to the
    # way it did before P12b -- not with seven null columns bolted on.  A Core
    # that has one always carries them, including on a claim the index has not
    # reached yet, so that "untagged" is visible rather than indistinguishable
    # from "no index anywhere".
    indexed = table_exists(store.connection)
    result = []
    for row in joined:
        row.pop("index_order", None)
        if not indexed:
            for field in (
                "index_aspect", "as_of", "as_of_basis", "importance",
                "dedupe_group_ref", "is_canonical", "index_entry_ref",
            ):
                row.pop(field, None)
        result.append(row)
        if len(result) >= limit:
            break
    return result


__all__ = [
    "CLAIM_STATUSES",
    "CompanyResearchViewError",
    "CompanyClaimQueryContext",
    "CompanyResearchViewValidationError",
    "PROJECTION_KIND",
    "annotate_with_index",
    "build_company_research_view",
    "query_company_research",
    "prepare_company_claim_query",
]
