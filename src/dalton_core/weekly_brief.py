"""Versioned weekly research briefs over exact industry research authority.

An IndustryEvidencePack is a point-in-time evidence view.  A WeeklyBriefIssue
adds a bounded reporting window, an exact delta against the prior issue,
formal-thesis availability, delivery receipts and content feedback.  It does
not summarize development work and it never generates a new investment claim.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .contracts import ThesisVersion
from .forecast_reconciliation import validate_forecast_reconciliation
from .industry_research import IndustryResearchAuthority, IndustryResearchError
from .store import DaltonStore, canonical_json, content_hash


SCHEMA_VERSION = "0.1"
ISSUE_SECTIONS = (
    "本期研究变化",
    "对现有观点的影响",
    "预测对账",
    "公司与 driver 分化",
    "证据缺口",
    "关键争议",
    "下期研究问题",
    "来源与 authority",
)
FEEDBACK_VERDICTS = frozenset({
    "read", "useful", "needs_more_evidence", "disagree", "revise",
})
FEEDBACK_TARGET_KINDS = frozenset({"brief", "company", "driver", "claim"})
_SCHEMA_PATH = Path(__file__).with_name("weekly_brief_schema.sql")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_THESIS_CONTENT_FIELDS = (
    "statement", "mechanism", "confidence", "implied_expectation",
    "claim_refs", "catalyst_refs", "falsifier_refs", "change_reason",
)


class WeeklyBriefError(RuntimeError):
    """Base error for weekly brief authority."""


class WeeklyBriefValidationError(WeeklyBriefError):
    """A request does not satisfy the closed contract."""


class WeeklyBriefConflict(WeeklyBriefError):
    """A request conflicts with immutable authority."""


class WeeklyBriefNotFound(WeeklyBriefError):
    """A referenced weekly brief object is absent."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WeeklyBriefValidationError(f"{name} must be non-empty text")
    return value.strip()


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if _HASH_RE.fullmatch(value) is None:
        raise WeeklyBriefValidationError(f"{name} must be lowercase SHA-256")
    return value


def _actor(value: Any, name: str = "actor_ref") -> str:
    value = _text(value, name)
    if ":" not in value or value.endswith(":"):
        raise WeeklyBriefValidationError(f"{name} must be a namespaced ref")
    return value


def _human(value: Any, name: str = "actor_ref") -> str:
    value = _actor(value, name)
    if not value.startswith("human:"):
        raise WeeklyBriefValidationError(f"{name} must use the human: namespace")
    return value


def _time(value: Any, name: str) -> tuple[str, datetime]:
    value = _text(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WeeklyBriefValidationError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise WeeklyBriefValidationError(f"{name} must include timezone")
    return value, parsed.astimezone(timezone.utc)


def _record(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = dict(value)
    wire["content_hash"] = content_hash(wire)
    return wire


def _canonical_record(raw: Any, name: str) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise WeeklyBriefConflict(f"{name} record is missing")
    try:
        wire = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WeeklyBriefConflict(f"{name} record is invalid JSON") from exc
    if not isinstance(wire, dict) or canonical_json(wire) != raw:
        raise WeeklyBriefConflict(f"{name} record is not canonical")
    base = dict(wire)
    declared = base.pop("content_hash", None)
    if declared is None or content_hash(base) != declared:
        raise WeeklyBriefConflict(f"{name} content hash drifted")
    return wire


def _question_key(company_ref: str, question: str) -> str:
    return "question:" + content_hash({"company_ref": company_ref, "question": question})


def _gap_key(driver_ref: str, metric_ref: str, company_ref: str, status: str) -> str:
    return "gap:" + content_hash({
        "driver_ref": driver_ref,
        "metric_ref": metric_ref,
        "company_ref": company_ref,
        "status": status,
    })


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


_RECONCILIATION_SUMMARY_FIELDS = (
    "company_ref", "metric_ref", "period", "forecast_line_version_ref",
    "claim_version_ref", "forecast_value", "actual_value", "unit", "currency",
    "deviation_percent", "direction", "band", "human_checkpoint",
)


def _reconciliation_summary(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ref": record["id"],
        "hash": record["content_hash"],
        "company_ref": record["subject_ref"],
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
    }


def _forecast_reconciliations_in_window(
    connection: sqlite3.Connection,
    company_refs: list[str],
    period_start: datetime,
    period_end: datetime,
) -> list[dict[str, Any]]:
    """P9c: exact reconciliation refs created inside the issue window."""

    if not _table_exists(connection, "forecast_reconciliations"):
        return []
    entries: list[dict[str, Any]] = []
    for row in connection.execute(
        "SELECT record_json, content_hash FROM forecast_reconciliations "
        "ORDER BY created_at, reconciliation_id"
    ).fetchall():
        record = validate_forecast_reconciliation(json.loads(row["record_json"]))
        if record["content_hash"] != row["content_hash"]:
            raise WeeklyBriefConflict("forecast reconciliation row hash drifted")
        created = datetime.fromisoformat(record["created_at"])
        if record["subject_ref"] not in company_refs or not period_start < created <= period_end:
            continue
        entries.append(_reconciliation_summary(record))
    return entries


def _replay_forecast_reconciliations(
    connection: sqlite3.Connection, entries: Any
) -> None:
    if entries is None:
        return
    if not isinstance(entries, list):
        raise WeeklyBriefConflict("weekly brief forecast reconciliations are malformed")
    for entry in entries:
        row = connection.execute(
            "SELECT record_json, content_hash FROM forecast_reconciliations "
            "WHERE reconciliation_id=?", (entry.get("ref"),),
        ).fetchone() if _table_exists(connection, "forecast_reconciliations") else None
        if row is None or row["content_hash"] != entry.get("hash"):
            raise WeeklyBriefConflict("weekly brief forecast reconciliation no longer replays")
        record = validate_forecast_reconciliation(json.loads(row["record_json"]))
        if _reconciliation_summary(record) != entry:
            raise WeeklyBriefConflict("weekly brief forecast reconciliation summary drifted")


def _snapshot_index(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    companies = sorted(item["company_ref"] for item in snapshot["coverage_universe"])
    claims = sorted(item["claim_version_ref"] for item in snapshot["claim_versions"])
    drivers = []
    for driver in snapshot["driver_scoreboard"]:
        drivers.append({
            "ref": driver["driver_ref"],
            "hash": content_hash(driver),
        })
    gaps: list[str] = []
    for metric in snapshot["metric_difference_matrix"]:
        for company in metric["companies"]:
            if company["status"] != "observed":
                gaps.append(_gap_key(
                    metric["driver_ref"], metric["metric_ref"],
                    company["company_ref"], company["status"],
                ))
    debates = sorted({
        item["debate_ref"]: content_hash(item) for item in snapshot["debates"]
    }.items())
    questions = []
    for company in snapshot["company_summaries"]:
        for question in company["open_questions"]:
            questions.append({
                "ref": _question_key(company["company_ref"], question),
                "company_ref": company["company_ref"],
                "question": question,
            })
    return {
        "company_refs": companies,
        "claim_version_refs": claims,
        "driver_hashes": sorted(drivers, key=lambda item: item["ref"]),
        "gap_refs": sorted(gaps),
        "debate_hashes": [{"ref": ref, "hash": value} for ref, value in debates],
        "open_questions": sorted(questions, key=lambda item: item["ref"]),
    }


def _change_summary(
    current: Mapping[str, Any],
    prior: Mapping[str, Any] | None,
    thesis_bindings: list[Mapping[str, Any]],
    prior_thesis_bindings: list[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    current_claims = set(current["claim_version_refs"])
    if prior is None:
        return {
            "is_baseline": True,
            "baseline_claim_version_refs": sorted(current_claims),
            "new_claim_version_refs": [],
            "carried_claim_version_refs": [],
            "removed_claim_version_refs": [],
            "changed_driver_refs": [],
            "new_gap_refs": [],
            "resolved_gap_refs": [],
            "new_question_refs": [],
            "resolved_question_refs": [],
            "changed_thesis_company_refs": [],
        }
    prior_claims = set(prior["claim_version_refs"])
    current_drivers = {item["ref"]: item["hash"] for item in current["driver_hashes"]}
    prior_drivers = {item["ref"]: item["hash"] for item in prior["driver_hashes"]}
    current_questions = {item["ref"] for item in current["open_questions"]}
    prior_questions = {item["ref"] for item in prior["open_questions"]}
    current_thesis = {
        item["company_ref"]: (item["status"], item.get("thesis_version_ref"))
        for item in thesis_bindings
    }
    prior_thesis = {
        item["company_ref"]: (item["status"], item.get("thesis_version_ref"))
        for item in (prior_thesis_bindings or [])
    }
    return {
        "is_baseline": False,
        "baseline_claim_version_refs": [],
        "new_claim_version_refs": sorted(current_claims - prior_claims),
        "carried_claim_version_refs": sorted(current_claims & prior_claims),
        "removed_claim_version_refs": sorted(prior_claims - current_claims),
        "changed_driver_refs": sorted(
            ref for ref in set(current_drivers) | set(prior_drivers)
            if current_drivers.get(ref) != prior_drivers.get(ref)
        ),
        "new_gap_refs": sorted(set(current["gap_refs"]) - set(prior["gap_refs"])),
        "resolved_gap_refs": sorted(set(prior["gap_refs"]) - set(current["gap_refs"])),
        "new_question_refs": sorted(current_questions - prior_questions),
        "resolved_question_refs": sorted(prior_questions - current_questions),
        "changed_thesis_company_refs": sorted(
            ref for ref in set(current_thesis) | set(prior_thesis)
            if current_thesis.get(ref) != prior_thesis.get(ref)
        ),
    }


class WeeklyBriefAuthority:
    """Publish, deliver and collect feedback on exact weekly research issues."""

    def __init__(self, store: DaltonStore, industry_research: IndustryResearchAuthority):
        if not isinstance(store, DaltonStore):
            raise TypeError("store must be DaltonStore")
        if not isinstance(industry_research, IndustryResearchAuthority):
            raise TypeError("industry_research must be IndustryResearchAuthority")
        if industry_research.store is not store:
            raise TypeError("weekly brief and industry research must share one Core")
        self.store = store
        self.industry_research = industry_research
        self.connection = store.connection
        self._authorized = False
        self.connection.create_function(
            "dalton_weekly_brief_authorized", 0, lambda: int(self._authorized)
        )
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self._authorized:
            raise RuntimeError("WeeklyBriefAuthority operation cannot be nested")
        self._authorized = True
        try:
            with self.store._transaction() as cur:
                yield cur
        finally:
            self._authorized = False

    @staticmethod
    def _request_hash(operation: str, request: Mapping[str, Any]) -> str:
        return content_hash({"operation": operation, "request": dict(request)})

    def _idem(
        self, cur: sqlite3.Cursor, key: str, operation: str, request_hash: str,
    ) -> dict[str, Any] | None:
        row = cur.execute(
            "SELECT * FROM weekly_brief_idempotency WHERE idempotency_key=?", (key,)
        ).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or row["request_hash"] != request_hash:
            raise WeeklyBriefConflict("idempotency key conflicts with prior request")
        return {**json.loads(row["result_json"]), "status": "duplicate"}

    @staticmethod
    def _save_idem(
        cur: sqlite3.Cursor, key: str, operation: str, request_hash: str,
        result: Mapping[str, Any], created_at: str,
    ) -> None:
        cur.execute(
            "INSERT INTO weekly_brief_idempotency"
            "(idempotency_key,operation,request_hash,result_json,created_at) VALUES(?,?,?,?,?)",
            (key, operation, request_hash, canonical_json(result), created_at),
        )

    @staticmethod
    def _issue_row(cur: sqlite3.Cursor, version_id: str) -> dict[str, Any]:
        row = cur.execute(
            "SELECT * FROM weekly_brief_issue_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise WeeklyBriefNotFound("weekly brief issue was not found")
        wire = _canonical_record(row["record_json"], "weekly brief issue")
        for field, column in (
            ("id", "version_id"), ("brief_ref", "brief_ref"),
            ("version", "version_number"), ("prior_version_ref", "prior_version_id"),
            ("industry_ref", "industry_ref"),
            ("evidence_pack_version_ref", "evidence_pack_version_ref"),
            ("evidence_pack_version_hash", "evidence_pack_version_hash"),
            ("snapshot_hash", "snapshot_hash"), ("markdown_hash", "markdown_hash"),
            ("actor_ref", "actor_ref"), ("created_at", "created_at"),
        ):
            if wire.get(field) != row[column]:
                raise WeeklyBriefConflict(f"weekly brief {field} column drifted")
        if wire["content_hash"] != row["content_hash"]:
            raise WeeklyBriefConflict("weekly brief content hash column drifted")
        if wire.get("period") != {
            "start": row["period_start"], "end": row["period_end"]
        }:
            raise WeeklyBriefConflict("weekly brief period columns drifted")
        return wire

    @staticmethod
    def _replay_thesis_bindings(
        cur: sqlite3.Cursor,
        company_refs: list[str],
        bindings: Any,
    ) -> list[dict[str, Any]]:
        if not isinstance(bindings, list) or len(bindings) != len(company_refs):
            raise WeeklyBriefConflict("weekly brief thesis bindings have invalid coverage")
        by_company: dict[str, dict[str, Any]] = {}
        for raw in bindings:
            if not isinstance(raw, dict):
                raise WeeklyBriefConflict("weekly brief thesis binding is invalid")
            company_ref = raw.get("company_ref")
            if company_ref in by_company or company_ref not in company_refs:
                raise WeeklyBriefConflict("weekly brief thesis binding company drifted")
            by_company[company_ref] = raw
        ordered = []
        for company_ref in company_refs:
            binding = by_company[company_ref]
            status = binding.get("status")
            if status == "insufficient":
                if binding != {
                    "company_ref": company_ref,
                    "status": "insufficient",
                    "thesis_ref": None,
                    "thesis_version_ref": None,
                    "thesis_version_hash": None,
                    "statement": None,
                    "confidence": None,
                }:
                    raise WeeklyBriefConflict(
                        "weekly brief insufficient thesis binding drifted"
                    )
            elif status == "current":
                thesis_ref = binding.get("thesis_ref")
                version_ref = binding.get("thesis_version_ref")
                version_hash = binding.get("thesis_version_hash")
                row = cur.execute(
                    "SELECT * FROM thesis_versions WHERE version_id=?",
                    (version_ref,),
                ).fetchone()
                if (
                    row is None
                    or row["thesis_id"] != thesis_ref
                    or row["content_hash"] != version_hash
                ):
                    raise WeeklyBriefConflict("weekly brief ThesisVersion binding drifted")
                try:
                    content = json.loads(row["content_json"])
                    validated = ThesisVersion.from_dict(content).to_dict()
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise WeeklyBriefConflict(
                        "weekly brief ThesisVersion content is invalid"
                    ) from exc
                if (
                    canonical_json(validated) != row["content_json"]
                    or content.get("id") != version_ref
                    or content.get("thesis_ref") != thesis_ref
                    or content.get("version") != row["version_number"]
                    or content.get("content_hash") != version_hash
                    or content_hash({
                        name: content[name] for name in _THESIS_CONTENT_FIELDS
                    }) != version_hash
                    or binding.get("statement") != content.get("statement")
                    or binding.get("confidence") != content.get("confidence")
                ):
                    raise WeeklyBriefConflict("weekly brief ThesisVersion content drifted")
                origin = cur.execute(
                    "SELECT c.company_ref FROM thesis_versions v "
                    "JOIN thesis_admission_decisions d "
                    "ON d.decision_id=v.admission_decision_id "
                    "JOIN thesis_admission_candidates c "
                    "ON c.candidate_id=d.candidate_id "
                    "WHERE v.thesis_id=? AND v.version_number=1",
                    (thesis_ref,),
                ).fetchone()
                if origin is None or origin["company_ref"] != company_ref:
                    raise WeeklyBriefConflict(
                        "weekly brief ThesisVersion company binding drifted"
                    )
            else:
                raise WeeklyBriefConflict("weekly brief thesis status is invalid")
            ordered.append(binding)
        return ordered

    def _thesis_bindings(
        self, cur: sqlite3.Cursor, company_refs: list[str], mapping: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        if not isinstance(mapping, Mapping):
            raise WeeklyBriefValidationError("company_thesis_refs must be an object")
        if set(mapping) - set(company_refs):
            raise WeeklyBriefValidationError("company_thesis_refs contains an outside company")
        result = []
        for company_ref in company_refs:
            thesis_ref = mapping.get(company_ref)
            if thesis_ref is None:
                result.append({
                    "company_ref": company_ref,
                    "status": "insufficient",
                    "thesis_ref": None,
                    "thesis_version_ref": None,
                    "thesis_version_hash": None,
                    "statement": None,
                    "confidence": None,
                })
                continue
            thesis_ref = _text(thesis_ref, "company_thesis_refs[]")
            pointer = cur.execute(
                "SELECT * FROM current_pointers WHERE thesis_id=?", (thesis_ref,)
            ).fetchone()
            if pointer is None:
                raise WeeklyBriefNotFound("mapped company Thesis pointer was not found")
            row = cur.execute(
                "SELECT * FROM thesis_versions WHERE version_id=?", (pointer["version_id"],)
            ).fetchone()
            if row is None or row["thesis_id"] != thesis_ref or row["content_hash"] != pointer["content_hash"]:
                raise WeeklyBriefConflict("current ThesisVersion pointer drifted")
            try:
                content = json.loads(row["content_json"])
                validated = ThesisVersion.from_dict(content).to_dict()
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise WeeklyBriefConflict("current ThesisVersion content is invalid") from exc
            if (
                canonical_json(validated) != row["content_json"]
                or content.get("id") != row["version_id"]
                or content.get("thesis_ref") != thesis_ref
                or content.get("version") != row["version_number"]
                or content.get("content_hash") != row["content_hash"]
                or content_hash({
                    name: content[name] for name in _THESIS_CONTENT_FIELDS
                }) != row["content_hash"]
            ):
                raise WeeklyBriefConflict("current ThesisVersion content drifted")
            origin = cur.execute(
                "SELECT c.company_ref FROM thesis_versions v "
                "JOIN thesis_admission_decisions d ON d.decision_id=v.admission_decision_id "
                "JOIN thesis_admission_candidates c ON c.candidate_id=d.candidate_id "
                "WHERE v.thesis_id=? AND v.version_number=1",
                (thesis_ref,),
            ).fetchone()
            if origin is None or origin["company_ref"] != company_ref:
                raise WeeklyBriefConflict("mapped ThesisVersion is not bound to the company")
            result.append({
                "company_ref": company_ref,
                "status": "current",
                "thesis_ref": thesis_ref,
                "thesis_version_ref": row["version_id"],
                "thesis_version_hash": row["content_hash"],
                "statement": _text(content.get("statement"), "thesis.statement"),
                "confidence": _text(content.get("confidence"), "thesis.confidence"),
            })
        return result

    def cycle_admission(self, cycle_id: str) -> dict[str, Any]:
        cycle_id = _text(cycle_id, "cycle_id")
        row = self.connection.execute(
            "SELECT * FROM weekly_brief_cycle_admissions WHERE cycle_id=?",
            (cycle_id,),
        ).fetchone()
        if row is None:
            raise WeeklyBriefNotFound("weekly brief cycle admission was not found")
        wire = _canonical_record(row["record_json"], "weekly brief cycle admission")
        for field, column in (
            ("id", "cycle_id"), ("plan_ref", "plan_ref"),
            ("plan_hash", "plan_hash"),
            ("policy_version_ref", "policy_version_ref"),
            ("policy_version_hash", "policy_version_hash"),
            ("scheduled_for", "scheduled_for"), ("brief_ref", "brief_ref"),
            ("issue_version_ref", "issue_version_ref"),
            ("prior_version_ref", "prior_version_ref"),
            ("evidence_pack_version_ref", "evidence_pack_version_ref"),
            ("destination_ref", "destination_ref"), ("actor_ref", "actor_ref"),
            ("created_at", "created_at"),
        ):
            if wire.get(field) != row[column]:
                raise WeeklyBriefConflict(
                    f"weekly brief cycle admission {field} column drifted"
                )
        if wire.get("period") != {
            "start": row["period_start"], "end": row["period_end"]
        }:
            raise WeeklyBriefConflict("weekly brief cycle admission period drifted")
        if wire.get("company_overlay_version_refs") != json.loads(
            row["company_overlay_version_refs_json"]
        ):
            raise WeeklyBriefConflict("weekly brief cycle overlay bindings drifted")
        if wire.get("company_thesis_refs") != json.loads(
            row["company_thesis_refs_json"]
        ):
            raise WeeklyBriefConflict("weekly brief cycle thesis bindings drifted")
        if wire["content_hash"] != row["content_hash"]:
            raise WeeklyBriefConflict("weekly brief cycle admission hash drifted")
        policy = self.connection.execute(
            "SELECT content_hash FROM governance_policy_versions "
            "WHERE policy_version_id=?",
            (wire["policy_version_ref"],),
        ).fetchone()
        if policy is None or policy["content_hash"] != wire["policy_version_hash"]:
            raise WeeklyBriefConflict(
                "weekly brief cycle admission policy authority drifted"
            )
        return wire

    def admit_scheduled_cycle(
        self,
        *,
        cycle_id: str,
        plan_ref: str,
        plan_hash: str,
        policy_version_ref: str,
        policy_version_hash: str,
        scheduled_for: str,
        period_start: str,
        period_end: str,
        brief_ref: str,
        issue_version_ref: str,
        prior_version_ref: str | None,
        evidence_pack_version_ref: str,
        company_overlay_version_refs: list[str],
        company_thesis_refs: Mapping[str, str],
        destination_ref: str,
        actor_ref: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        cycle_id = _text(cycle_id, "cycle_id")
        plan_ref = _text(plan_ref, "plan_ref")
        plan_hash = _hash(plan_hash, "plan_hash")
        policy_version_ref = _text(policy_version_ref, "policy_version_ref")
        policy_version_hash = _hash(policy_version_hash, "policy_version_hash")
        scheduled_for, scheduled = _time(scheduled_for, "scheduled_for")
        period_start, start = _time(period_start, "period_start")
        period_end, end = _time(period_end, "period_end")
        if end != scheduled or end <= start or end - start > timedelta(days=8):
            raise WeeklyBriefValidationError(
                "scheduled weekly brief period must end at schedule and span >0 and <=8 days"
            )
        brief_ref = _text(brief_ref, "brief_ref")
        issue_version_ref = _text(issue_version_ref, "issue_version_ref")
        evidence_pack_version_ref = _text(
            evidence_pack_version_ref, "evidence_pack_version_ref"
        )
        if (
            not isinstance(company_overlay_version_refs, list)
            or not company_overlay_version_refs
        ):
            raise WeeklyBriefValidationError(
                "company_overlay_version_refs must be a non-empty array"
            )
        overlays = [
            _text(value, "company_overlay_version_refs[]")
            for value in company_overlay_version_refs
        ]
        if len(overlays) != len(set(overlays)):
            raise WeeklyBriefValidationError(
                "company_overlay_version_refs must be unique"
            )
        if not isinstance(company_thesis_refs, Mapping):
            raise WeeklyBriefValidationError("company_thesis_refs must be an object")
        theses = {
            _text(key, "company_thesis_refs key"): _text(value, "company_thesis_refs value")
            for key, value in company_thesis_refs.items()
        }
        destination_ref = _text(destination_ref, "destination_ref")
        actor_ref = _text(actor_ref, "actor_ref")
        if actor_ref != "core":
            raise WeeklyBriefValidationError(
                "scheduled weekly brief admission requires the core actor"
            )
        idempotency_key = _text(idempotency_key, "idempotency_key")
        if prior_version_ref is not None:
            prior_version_ref = _text(prior_version_ref, "prior_version_ref")
        request = {
            "cycle_id": cycle_id, "plan_ref": plan_ref, "plan_hash": plan_hash,
            "policy_version_ref": policy_version_ref,
            "policy_version_hash": policy_version_hash,
            "scheduled_for": scheduled_for, "period_start": period_start,
            "period_end": period_end, "brief_ref": brief_ref,
            "issue_version_ref": issue_version_ref,
            "prior_version_ref": prior_version_ref,
            "evidence_pack_version_ref": evidence_pack_version_ref,
            "company_overlay_version_refs": overlays,
            "company_thesis_refs": theses, "destination_ref": destination_ref,
            "actor_ref": actor_ref,
        }
        request_hash = self._request_hash("admit_weekly_brief_cycle", request)
        created_at = _now()
        with self._transaction() as cur:
            duplicate = self._idem(
                cur, idempotency_key, "admit_weekly_brief_cycle", request_hash
            )
            if duplicate is not None:
                return duplicate
            latest = cur.execute(
                "SELECT version_id FROM weekly_brief_issue_versions "
                "WHERE brief_ref=? ORDER BY version_number DESC LIMIT 1",
                (brief_ref,),
            ).fetchone()
            expected_prior = None if latest is None else latest["version_id"]
            if prior_version_ref != expected_prior:
                raise WeeklyBriefConflict(
                    "scheduled cycle prior version is not the latest weekly issue"
                )
            wire = _record({
                "schema_version": SCHEMA_VERSION, "id": cycle_id,
                "plan_ref": plan_ref, "plan_hash": plan_hash,
                "policy_version_ref": policy_version_ref,
                "policy_version_hash": policy_version_hash,
                "scheduled_for": scheduled_for,
                "period": {"start": period_start, "end": period_end},
                "brief_ref": brief_ref, "issue_version_ref": issue_version_ref,
                "prior_version_ref": prior_version_ref,
                "evidence_pack_version_ref": evidence_pack_version_ref,
                "company_overlay_version_refs": overlays,
                "company_thesis_refs": theses,
                "destination_ref": destination_ref,
                "actor_ref": actor_ref, "created_at": created_at,
            })
            try:
                cur.execute(
                    "INSERT INTO weekly_brief_cycle_admissions("
                    "cycle_id,plan_ref,plan_hash,policy_version_ref,policy_version_hash,"
                    "scheduled_for,period_start,period_end,brief_ref,issue_version_ref,"
                    "prior_version_ref,evidence_pack_version_ref,"
                    "company_overlay_version_refs_json,company_thesis_refs_json,"
                    "destination_ref,record_json,content_hash,actor_ref,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        cycle_id, plan_ref, plan_hash, policy_version_ref,
                        policy_version_hash, scheduled_for, period_start, period_end,
                        brief_ref, issue_version_ref, prior_version_ref,
                        evidence_pack_version_ref, canonical_json(overlays),
                        canonical_json(theses), destination_ref, canonical_json(wire),
                        wire["content_hash"], actor_ref, created_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise WeeklyBriefConflict(
                    "weekly brief cycle admission already exists"
                ) from exc
            result = {"status": "fresh", **wire}
            self._save_idem(
                cur, idempotency_key, "admit_weekly_brief_cycle", request_hash,
                result, created_at,
            )
            return result

    def publish_scheduled_issue(
        self, cycle_id: str, *, actor_ref: str, idempotency_key: str
    ) -> dict[str, Any]:
        actor_ref = _text(actor_ref, "actor_ref")
        if actor_ref != "core":
            raise WeeklyBriefValidationError(
                "scheduled weekly brief publication requires the core actor"
            )
        admission = self.cycle_admission(cycle_id)
        return self._publish_issue(
            admission["brief_ref"],
            period_start=admission["period"]["start"],
            period_end=admission["period"]["end"],
            evidence_pack_version_id=admission["evidence_pack_version_ref"],
            company_overlay_version_ids=admission["company_overlay_version_refs"],
            company_thesis_refs=admission["company_thesis_refs"],
            actor_ref=actor_ref,
            version_id=admission["issue_version_ref"],
            prior_version_ref=admission["prior_version_ref"],
            idempotency_key=idempotency_key,
            operation="publish_scheduled_weekly_brief",
        )

    def publish_issue(
        self,
        brief_ref: str,
        *,
        period_start: str,
        period_end: str,
        evidence_pack_version_id: str,
        company_overlay_version_ids: list[str],
        company_thesis_refs: Mapping[str, str],
        actor_ref: str,
        version_id: str,
        prior_version_ref: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._publish_issue(
            brief_ref,
            period_start=period_start,
            period_end=period_end,
            evidence_pack_version_id=evidence_pack_version_id,
            company_overlay_version_ids=company_overlay_version_ids,
            company_thesis_refs=company_thesis_refs,
            actor_ref=_human(actor_ref),
            version_id=version_id,
            prior_version_ref=prior_version_ref,
            idempotency_key=idempotency_key,
            operation="publish_weekly_brief",
        )

    def _publish_issue(
        self,
        brief_ref: str,
        *,
        period_start: str,
        period_end: str,
        evidence_pack_version_id: str,
        company_overlay_version_ids: list[str],
        company_thesis_refs: Mapping[str, str],
        actor_ref: str,
        version_id: str,
        prior_version_ref: str | None,
        idempotency_key: str,
        operation: str,
    ) -> dict[str, Any]:
        brief_ref = _text(brief_ref, "brief_ref")
        version_id = _text(version_id, "version_id")
        evidence_pack_version_id = _text(
            evidence_pack_version_id, "evidence_pack_version_id"
        )
        if not isinstance(company_overlay_version_ids, list) or not company_overlay_version_ids:
            raise WeeklyBriefValidationError(
                "company_overlay_version_ids must be a non-empty array"
            )
        overlay_ids = [_text(item, "company_overlay_version_ids[]") for item in company_overlay_version_ids]
        if len(overlay_ids) != len(set(overlay_ids)):
            raise WeeklyBriefValidationError("company_overlay_version_ids must be unique")
        period_start, start = _time(period_start, "period_start")
        period_end, end = _time(period_end, "period_end")
        if end <= start or end - start > timedelta(days=8):
            raise WeeklyBriefValidationError("weekly brief period must span >0 and <=8 days")
        actor_ref = _text(actor_ref, "actor_ref")
        if actor_ref != "core":
            actor_ref = _actor(actor_ref)
        idempotency_key = _text(idempotency_key, "idempotency_key")
        if prior_version_ref is not None:
            prior_version_ref = _text(prior_version_ref, "prior_version_ref")
        if not isinstance(company_thesis_refs, Mapping):
            raise WeeklyBriefValidationError("company_thesis_refs must be an object")

        try:
            snapshot = self.industry_research.industry_brief_snapshot(
                evidence_pack_version_id, overlay_ids
            )
            markdown = self.industry_research.render_industry_brief_markdown(
                evidence_pack_version_id, overlay_ids
            )
        except IndustryResearchError as exc:
            raise WeeklyBriefConflict(str(exc)) from exc
        if markdown["snapshot_hash"] != snapshot["content_hash"]:
            raise WeeklyBriefConflict("industry snapshot and markdown drifted")
        index = _snapshot_index(snapshot)
        reconciliations = _forecast_reconciliations_in_window(
            self.connection, index["company_refs"], start, end
        )
        created_at = _now()
        request = {
            "brief_ref": brief_ref,
            "period_start": period_start,
            "period_end": period_end,
            "evidence_pack_version_id": evidence_pack_version_id,
            "company_overlay_version_ids": overlay_ids,
            "company_thesis_refs": dict(company_thesis_refs),
            "actor_ref": actor_ref,
            "version_id": version_id,
            "prior_version_ref": prior_version_ref,
        }
        request_hash = self._request_hash(operation, request)
        with self._transaction() as cur:
            duplicate = self._idem(
                cur, idempotency_key, operation, request_hash
            )
            if duplicate is not None:
                return duplicate
            latest = cur.execute(
                "SELECT version_id,version_number FROM weekly_brief_issue_versions "
                "WHERE brief_ref=? ORDER BY version_number DESC LIMIT 1", (brief_ref,)
            ).fetchone()
            expected_version = 1 if latest is None else int(latest["version_number"]) + 1
            expected_prior = None if latest is None else latest["version_id"]
            if prior_version_ref != expected_prior:
                raise WeeklyBriefConflict("weekly brief prior-version chain is not latest")
            if cur.execute(
                "SELECT 1 FROM weekly_brief_issue_versions WHERE version_id=?", (version_id,)
            ).fetchone():
                raise WeeklyBriefConflict("weekly brief version_id already exists")
            prior_issue = (
                None if expected_prior is None else self._issue_row(cur, expected_prior)
            )
            thesis_bindings = self._thesis_bindings(
                cur, index["company_refs"], company_thesis_refs
            )
            changes = _change_summary(
                index,
                None if prior_issue is None else prior_issue["content_index"],
                thesis_bindings,
                None if prior_issue is None else prior_issue["thesis_bindings"],
            )
            issue = _record({
                "schema_version": SCHEMA_VERSION,
                "id": version_id,
                "created_at": created_at,
                "brief_ref": brief_ref,
                "version": expected_version,
                "prior_version_ref": expected_prior,
                "industry_ref": snapshot["industry_ref"],
                "period": {"start": period_start, "end": period_end},
                "evidence_pack_version_ref": snapshot["evidence_pack_version_ref"],
                "evidence_pack_version_hash": snapshot["evidence_pack_version_hash"],
                "company_overlay_versions": snapshot["company_overlay_versions"],
                "snapshot_hash": snapshot["content_hash"],
                "base_markdown_hash": markdown["content_hash"],
                "markdown_hash": hashlib.sha256(
                    markdown["body"].encode("utf-8")
                ).hexdigest(),
                "content_index": index,
                "change_summary": changes,
                "thesis_bindings": thesis_bindings,
                "forecast_reconciliations": reconciliations,
                "sections": list(ISSUE_SECTIONS),
                "actor_ref": actor_ref,
            })
            cur.execute(
                "INSERT INTO weekly_brief_issue_versions"
                "(version_id,brief_ref,version_number,prior_version_id,industry_ref,"
                "evidence_pack_version_ref,evidence_pack_version_hash,snapshot_hash,markdown_hash,"
                "period_start,period_end,record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    issue["id"], brief_ref, expected_version, expected_prior,
                    issue["industry_ref"], issue["evidence_pack_version_ref"],
                    issue["evidence_pack_version_hash"], issue["snapshot_hash"],
                    issue["markdown_hash"], period_start, period_end,
                    canonical_json(issue), issue["content_hash"], actor_ref, created_at,
                ),
            )
            if latest is None:
                cur.execute(
                    "INSERT INTO weekly_brief_issue_pointer"
                    "(brief_ref,version_id,version_number,content_hash,updated_at) VALUES(?,?,?,?,?)",
                    (brief_ref, version_id, expected_version, issue["content_hash"], created_at),
                )
            else:
                cur.execute(
                    "UPDATE weekly_brief_issue_pointer SET version_id=?,version_number=?,"
                    "content_hash=?,updated_at=? WHERE brief_ref=?",
                    (version_id, expected_version, issue["content_hash"], created_at, brief_ref),
                )
            result = {"status": "fresh", **issue}
            self._save_idem(
                cur, idempotency_key, operation, request_hash,
                result, created_at,
            )
            return result

    def issue(self, version_id: str) -> dict[str, Any]:
        version_id = _text(version_id, "version_id")
        cur = self.connection.cursor()
        wire = self._issue_row(cur, version_id)
        try:
            snapshot = self.industry_research.industry_brief_snapshot(
                wire["evidence_pack_version_ref"],
                [item["version_ref"] for item in wire["company_overlay_versions"]],
            )
            markdown = self.industry_research.render_industry_brief_markdown(
                wire["evidence_pack_version_ref"],
                [item["version_ref"] for item in wire["company_overlay_versions"]],
            )
        except IndustryResearchError as exc:
            raise WeeklyBriefConflict(str(exc)) from exc
        if (
            snapshot["content_hash"] != wire["snapshot_hash"]
            or _snapshot_index(snapshot) != wire["content_index"]
            or markdown["content_hash"] != wire["base_markdown_hash"]
            or hashlib.sha256(markdown["body"].encode("utf-8")).hexdigest()
            != wire["markdown_hash"]
        ):
            raise WeeklyBriefConflict("weekly brief source authority no longer replays")
        thesis_bindings = self._replay_thesis_bindings(
            cur, wire["content_index"]["company_refs"], wire["thesis_bindings"]
        )
        _replay_forecast_reconciliations(self.connection, wire.get("forecast_reconciliations"))
        prior = None
        prior_thesis = None
        if wire["prior_version_ref"] is not None:
            prior_wire = self._issue_row(cur, wire["prior_version_ref"])
            if (
                prior_wire["brief_ref"] != wire["brief_ref"]
                or prior_wire["version"] != wire["version"] - 1
            ):
                raise WeeklyBriefConflict("weekly brief prior-version chain drifted")
            prior = prior_wire["content_index"]
            prior_thesis = self._replay_thesis_bindings(
                cur,
                prior_wire["content_index"]["company_refs"],
                prior_wire["thesis_bindings"],
            )
        elif wire["version"] != 1:
            raise WeeklyBriefConflict("weekly brief version chain is incomplete")
        expected_changes = _change_summary(
            wire["content_index"], prior, thesis_bindings, prior_thesis
        )
        if wire["change_summary"] != expected_changes:
            raise WeeklyBriefConflict("weekly brief change summary drifted")
        return wire

    def render_markdown(self, version_id: str) -> dict[str, Any]:
        issue = self.issue(version_id)
        snapshot = self.industry_research.industry_brief_snapshot(
            issue["evidence_pack_version_ref"],
            [item["version_ref"] for item in issue["company_overlay_versions"]],
        )
        claims = {
            item["claim_version_ref"]: item for item in snapshot["claim_versions"]
        }
        change = issue["change_summary"]
        lines = [
            f"# {snapshot['title']}｜每周研究 Brief",
            "",
            f"- 研究窗口：{issue['period']['start']} 至 {issue['period']['end']}",
            f"- Issue：{issue['id']} ({issue['content_hash']})",
            f"- Evidence Pack：{issue['evidence_pack_version_ref']} ({issue['evidence_pack_version_hash']})",
            "",
            "## 本期研究变化",
            "",
        ]
        if change["is_baseline"]:
            lines.append(
                "- 本期是首次基线，不把此前累积的正式 Claim 冒充为本周新增。"
            )
            selected_claims = change["baseline_claim_version_refs"]
            label = "基线 Claim"
        else:
            lines.extend([
                f"- 新增正式 Claim：{len(change['new_claim_version_refs'])} 条。",
                f"- 延续正式 Claim：{len(change['carried_claim_version_refs'])} 条。",
                f"- 移出当前 pack：{len(change['removed_claim_version_refs'])} 条。",
                f"- 发生变化的 driver：{len(change['changed_driver_refs'])} 个。",
                f"- 新增 / 已解决证据缺口：{len(change['new_gap_refs'])} / {len(change['resolved_gap_refs'])}。",
            ])
            selected_claims = change["new_claim_version_refs"]
            label = "新增 Claim"
        for ref in selected_claims:
            claim = claims.get(ref)
            if claim is None:
                raise WeeklyBriefConflict("weekly change references a claim outside snapshot")
            value = (
                "qualitative" if claim.get("value") is None
                else f"{claim['value']} {claim.get('unit') or ''}".strip()
            )
            lines.append(
                f"- {label}｜{claim['subject_ref']}｜{claim['metric_or_aspect']}｜"
                f"{value}｜{claim['period']}｜{ref}"
            )

        lines.extend(["", "## 对现有观点的影响", ""])
        for binding in issue["thesis_bindings"]:
            if binding["status"] == "insufficient":
                lines.append(
                    f"- {binding['company_ref']}：insufficient——尚无正式当前 ThesisVersion，"
                    "不能断言本期证据改变了投资观点。"
                )
            else:
                changed = binding["company_ref"] in change["changed_thesis_company_refs"]
                lines.append(
                    f"- {binding['company_ref']}：{binding['statement']} "
                    f"(confidence={binding['confidence']}; thesis={binding['thesis_version_ref']}; "
                    f"本期版本变化={'yes' if changed else 'no'})"
                )

        lines.extend(["", "## 预测对账", ""])
        tickers = {
            item["company_ref"]: item["ticker"] for item in snapshot["coverage_universe"]
        }
        reconciliations = issue.get("forecast_reconciliations") or []
        if not reconciliations:
            lines.append("- 本期没有预测线被实际数对账；没有实际数落库就不写预测偏差。")
        for item in reconciliations:
            ticker = tickers.get(item["company_ref"], item["company_ref"])
            checkpoint = (
                "；触发 forecast_overturn 人工检查点，预测未自动修改"
                if item["human_checkpoint"] == "forecast_overturn" else ""
            )
            lines.append(
                f"- {ticker}｜{item['metric_ref']}｜{item['period']}｜"
                f"预测 {item['forecast_value']} {item['unit']} {item['currency']}｜"
                f"实际 {item['actual_value']}｜偏差 {item['deviation_percent']}%｜"
                f"{item['direction']}｜{item['band']}{checkpoint}｜{item['ref']}"
            )

        lines.extend(["", "## 公司与 driver 分化", ""])
        for driver in snapshot["driver_scoreboard"]:
            lines.append(f"### {driver['label']}")
            lines.append("")
            for company in driver["companies"]:
                observed = ", ".join(company["observed_metric_refs"]) or "none"
                unresolved = ", ".join(company["unresolved_metric_refs"]) or "none"
                lines.append(
                    f"- {company['ticker']}：stance={company['stance']}；"
                    f"observed={observed}；unresolved={unresolved}"
                )
                lines.extend(
                    f"  - 差异：{item}" for item in company["differentiators"]
                )
                lines.extend(
                    f"  - 观察点：{item}" for item in company["watchpoints"]
                )
            lines.append("")

        lines.extend(["## 证据缺口", ""])
        gaps = 0
        for metric in snapshot["metric_difference_matrix"]:
            for company in metric["companies"]:
                if company["status"] == "observed":
                    continue
                gaps += 1
                lines.append(
                    f"- {company['ticker']}｜{metric['metric_ref']}｜{company['status']}｜"
                    f"{company['rationale']}"
                )
        if gaps == 0:
            lines.append("- 当前 issue 的闭合 metric matrix 没有未覆盖单元格。")

        lines.extend(["", "## 关键争议", ""])
        for debate in snapshot["debates"]:
            lines.append(f"- {debate['question']}（status={debate['status']}）")
            for position in debate["positions"]:
                lines.append(
                    f"  - {position['stance']}：{position['label']}｜"
                    f"claims={', '.join(position['claim_version_refs'])}"
                )

        lines.extend(["", "## 下期研究问题", ""])
        for company in snapshot["company_summaries"]:
            for question in company["open_questions"]:
                lines.append(f"- {company['ticker']}：{question}")

        lines.extend(["", "## 来源与 authority", ""])
        for source in snapshot["source_authorities"]:
            lines.append(
                f"- {source['source_ref']}｜evidence={source['evidence_version_ref']}｜"
                f"retrieved={source['retrieved_at']}｜hash={source['content_hash']}"
            )
        body = "\n".join(lines).rstrip() + "\n"
        return _record({
            "schema_version": SCHEMA_VERSION,
            "projection_kind": "weekly_brief_markdown",
            "issue_version_ref": issue["id"],
            "issue_version_hash": issue["content_hash"],
            "snapshot_hash": issue["snapshot_hash"],
            "media_type": "text/markdown",
            "body": body,
        })

    def record_delivery(
        self,
        *,
        issue_version_ref: str,
        issue_version_hash: str,
        destination_ref: str,
        external_message_ref: str,
        artifact_sha256: str,
        delivered_at: str,
        delivery_id: str,
        actor_ref: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._record_delivery(
            issue_version_ref=issue_version_ref,
            issue_version_hash=issue_version_hash,
            destination_ref=destination_ref,
            external_message_ref=external_message_ref,
            artifact_sha256=artifact_sha256,
            delivered_at=delivered_at,
            delivery_id=delivery_id,
            actor_ref=_human(actor_ref),
            idempotency_key=idempotency_key,
            operation="record_weekly_brief_delivery",
        )

    def record_scheduled_delivery(
        self,
        *,
        cycle_id: str,
        issue_version_ref: str,
        issue_version_hash: str,
        destination_ref: str,
        external_message_ref: str,
        artifact_sha256: str,
        delivered_at: str,
        delivery_id: str,
        actor_ref: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        actor_ref = _text(actor_ref, "actor_ref")
        if actor_ref != "core":
            raise WeeklyBriefValidationError(
                "scheduled weekly brief delivery requires the core actor"
            )
        admission = self.cycle_admission(cycle_id)
        if (
            admission["issue_version_ref"] != issue_version_ref
            or admission["destination_ref"] != destination_ref
        ):
            raise WeeklyBriefConflict(
                "scheduled delivery is outside the admitted issue or destination"
            )
        return self._record_delivery(
            issue_version_ref=issue_version_ref,
            issue_version_hash=issue_version_hash,
            destination_ref=destination_ref,
            external_message_ref=external_message_ref,
            artifact_sha256=artifact_sha256,
            delivered_at=delivered_at,
            delivery_id=delivery_id,
            actor_ref=actor_ref,
            idempotency_key=idempotency_key,
            operation="record_scheduled_weekly_brief_delivery",
        )

    def _record_delivery(
        self,
        *,
        issue_version_ref: str,
        issue_version_hash: str,
        destination_ref: str,
        external_message_ref: str,
        artifact_sha256: str,
        delivered_at: str,
        delivery_id: str,
        actor_ref: str,
        idempotency_key: str,
        operation: str,
    ) -> dict[str, Any]:
        issue_version_ref = _text(issue_version_ref, "issue_version_ref")
        issue_version_hash = _hash(issue_version_hash, "issue_version_hash")
        destination_ref = _text(destination_ref, "destination_ref")
        external_message_ref = _text(external_message_ref, "external_message_ref")
        artifact_sha256 = _hash(artifact_sha256, "artifact_sha256")
        delivered_at, _ = _time(delivered_at, "delivered_at")
        delivery_id = _text(delivery_id, "delivery_id")
        actor_ref = _text(actor_ref, "actor_ref")
        if actor_ref != "core":
            actor_ref = _actor(actor_ref)
        idempotency_key = _text(idempotency_key, "idempotency_key")
        issue = self.issue(issue_version_ref)
        if issue["content_hash"] != issue_version_hash:
            raise WeeklyBriefConflict("delivery issue hash binding failed")
        rendered = self.render_markdown(issue_version_ref)
        if hashlib.sha256(rendered["body"].encode("utf-8")).hexdigest() != artifact_sha256:
            raise WeeklyBriefConflict("delivery artifact hash is not the exact weekly brief")
        request = {
            "issue_version_ref": issue_version_ref,
            "issue_version_hash": issue_version_hash,
            "destination_ref": destination_ref,
            "external_message_ref": external_message_ref,
            "artifact_sha256": artifact_sha256,
            "delivered_at": delivered_at,
            "delivery_id": delivery_id,
            "actor_ref": actor_ref,
        }
        request_hash = self._request_hash(operation, request)
        with self._transaction() as cur:
            duplicate = self._idem(
                cur, idempotency_key, operation, request_hash
            )
            if duplicate is not None:
                return duplicate
            wire = _record({
                "schema_version": SCHEMA_VERSION,
                "id": delivery_id,
                "issue_version_ref": issue_version_ref,
                "issue_version_hash": issue_version_hash,
                "destination_ref": destination_ref,
                "external_message_ref": external_message_ref,
                "artifact_sha256": artifact_sha256,
                "delivered_at": delivered_at,
                "actor_ref": actor_ref,
            })
            try:
                cur.execute(
                    "INSERT INTO weekly_brief_deliveries"
                    "(delivery_id,issue_version_ref,issue_version_hash,destination_ref,"
                    "external_message_ref,artifact_sha256,record_json,content_hash,actor_ref,delivered_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        delivery_id, issue_version_ref, issue_version_hash,
                        destination_ref, external_message_ref, artifact_sha256,
                        canonical_json(wire), wire["content_hash"], actor_ref, delivered_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise WeeklyBriefConflict("weekly brief delivery already exists") from exc
            result = {"status": "fresh", **wire}
            self._save_idem(
                cur, idempotency_key, operation, request_hash,
                result, delivered_at,
            )
            return result

    def record_feedback(
        self,
        *,
        issue_version_ref: str,
        issue_version_hash: str,
        verdict: str,
        target_kind: str,
        target_ref: str,
        notes: str,
        feedback_id: str,
        prior_feedback_ref: str | None,
        subject_ref: str,
        actor_ref: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        issue_version_ref = _text(issue_version_ref, "issue_version_ref")
        issue_version_hash = _hash(issue_version_hash, "issue_version_hash")
        if verdict not in FEEDBACK_VERDICTS:
            raise WeeklyBriefValidationError("weekly brief feedback verdict is invalid")
        if target_kind not in FEEDBACK_TARGET_KINDS:
            raise WeeklyBriefValidationError("weekly brief feedback target_kind is invalid")
        target_ref = _text(target_ref, "target_ref")
        notes = _text(notes, "notes")
        feedback_id = _text(feedback_id, "feedback_id")
        subject_ref = _human(subject_ref, "subject_ref")
        actor_ref = _actor(actor_ref)
        if actor_ref.startswith("human:") and actor_ref != subject_ref:
            raise WeeklyBriefValidationError(
                "direct human feedback subject_ref must match actor_ref"
            )
        idempotency_key = _text(idempotency_key, "idempotency_key")
        if prior_feedback_ref is not None:
            prior_feedback_ref = _text(prior_feedback_ref, "prior_feedback_ref")
        issue = self.issue(issue_version_ref)
        if issue["content_hash"] != issue_version_hash:
            raise WeeklyBriefConflict("feedback issue hash binding failed")
        targets = {
            "brief": {issue["id"]},
            "company": set(issue["content_index"]["company_refs"]),
            "driver": {item["ref"] for item in issue["content_index"]["driver_hashes"]},
            "claim": set(issue["content_index"]["claim_version_refs"]),
        }
        if target_ref not in targets[target_kind]:
            raise WeeklyBriefConflict("feedback target is outside the exact weekly issue")
        created_at = _now()
        request = {
            "issue_version_ref": issue_version_ref,
            "issue_version_hash": issue_version_hash,
            "verdict": verdict,
            "target_kind": target_kind,
            "target_ref": target_ref,
            "notes": notes,
            "feedback_id": feedback_id,
            "prior_feedback_ref": prior_feedback_ref,
            "subject_ref": subject_ref,
            "actor_ref": actor_ref,
        }
        request_hash = self._request_hash("record_weekly_brief_feedback", request)
        with self._transaction() as cur:
            duplicate = self._idem(
                cur, idempotency_key, "record_weekly_brief_feedback", request_hash
            )
            if duplicate is not None:
                return duplicate
            if prior_feedback_ref is not None:
                prior = cur.execute(
                    "SELECT * FROM weekly_brief_feedback WHERE feedback_id=?",
                    (prior_feedback_ref,),
                ).fetchone()
                if (
                    prior is None
                    or prior["issue_version_ref"] != issue_version_ref
                    or prior["target_kind"] != target_kind
                    or prior["target_ref"] != target_ref
                    or prior["subject_ref"] != subject_ref
                    or prior["actor_ref"] != actor_ref
                ):
                    raise WeeklyBriefConflict("prior feedback chain is incompatible")
            wire = _record({
                "schema_version": SCHEMA_VERSION,
                "id": feedback_id,
                "created_at": created_at,
                "issue_version_ref": issue_version_ref,
                "issue_version_hash": issue_version_hash,
                "verdict": verdict,
                "target_kind": target_kind,
                "target_ref": target_ref,
                "notes": notes,
                "prior_feedback_ref": prior_feedback_ref,
                "subject_ref": subject_ref,
                "actor_ref": actor_ref,
            })
            try:
                cur.execute(
                    "INSERT INTO weekly_brief_feedback"
                    "(feedback_id,issue_version_ref,issue_version_hash,verdict,target_kind,"
                    "target_ref,prior_feedback_ref,subject_ref,record_json,content_hash,actor_ref,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        feedback_id, issue_version_ref, issue_version_hash, verdict,
                        target_kind, target_ref, prior_feedback_ref, subject_ref,
                        canonical_json(wire),
                        wire["content_hash"], actor_ref, created_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise WeeklyBriefConflict("weekly brief feedback already exists") from exc
            result = {"status": "fresh", **wire}
            self._save_idem(
                cur, idempotency_key, "record_weekly_brief_feedback", request_hash,
                result, created_at,
            )
            return result

    def feedback(self, issue_version_ref: str) -> list[dict[str, Any]]:
        issue = self.issue(issue_version_ref)
        result = []
        for row in self.connection.execute(
            "SELECT * FROM weekly_brief_feedback WHERE issue_version_ref=? "
            "ORDER BY created_at,feedback_id", (issue["id"],)
        ).fetchall():
            wire = _canonical_record(row["record_json"], "weekly brief feedback")
            if wire["content_hash"] != row["content_hash"]:
                raise WeeklyBriefConflict("weekly brief feedback hash column drifted")
            result.append(wire)
        return result

    def integrity_report(self) -> dict[str, Any]:
        issues = []
        for table in (
            "weekly_brief_cycle_admissions", "weekly_brief_issue_versions",
            "weekly_brief_deliveries", "weekly_brief_feedback",
        ):
            for row in self.connection.execute(f"SELECT * FROM {table}").fetchall():
                try:
                    wire = _canonical_record(row["record_json"], table)
                    if wire["content_hash"] != row["content_hash"]:
                        issues.append(f"{table}: content hash column drifted")
                except WeeklyBriefError as exc:
                    issues.append(f"{table}: {exc}")
        for pointer in self.connection.execute(
            "SELECT * FROM weekly_brief_issue_pointer"
        ).fetchall():
            latest = self.connection.execute(
                "SELECT version_id,version_number,content_hash FROM weekly_brief_issue_versions "
                "WHERE brief_ref=? ORDER BY version_number DESC LIMIT 1",
                (pointer["brief_ref"],),
            ).fetchone()
            if latest is None or any(
                pointer[field] != latest[field]
                for field in ("version_id", "version_number", "content_hash")
            ):
                issues.append("weekly_brief_issue_pointer: pointer is not latest")
        if self.connection.execute("PRAGMA foreign_key_check").fetchall():
            issues.append("foreign_key_check failed")
        return {
            "ok": not issues,
            "issues": issues,
            "issue_versions": self.connection.execute(
                "SELECT COUNT(*) FROM weekly_brief_issue_versions"
            ).fetchone()[0],
            "cycle_admissions": self.connection.execute(
                "SELECT COUNT(*) FROM weekly_brief_cycle_admissions"
            ).fetchone()[0],
            "deliveries": self.connection.execute(
                "SELECT COUNT(*) FROM weekly_brief_deliveries"
            ).fetchone()[0],
            "feedback": self.connection.execute(
                "SELECT COUNT(*) FROM weekly_brief_feedback"
            ).fetchone()[0],
        }


__all__ = [
    "FEEDBACK_TARGET_KINDS", "FEEDBACK_VERDICTS", "ISSUE_SECTIONS",
    "WeeklyBriefAuthority", "WeeklyBriefConflict", "WeeklyBriefError",
    "WeeklyBriefNotFound", "WeeklyBriefValidationError",
]
