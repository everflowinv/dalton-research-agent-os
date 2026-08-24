"""Immutable SEC statement snapshots with Decimal reconciliation.

The worker never fetches a filing. It consumes one exact raw Company Facts
artifact, verifies its byte hash, selects only human-admitted taxonomy tags on
one accession, and persists flat fact rows. Labels are provenance only: a
model-suggested synonym cannot become a formal fact until a human publishes a
new concept-set version.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .contracts import WorkOrder
from .store import canonical_json, content_hash


SCHEMA_VERSION = "0.1"
STATEMENT_SNAPSHOT_CAPABILITY = "capability:dalton:local:statement-snapshot"
STATEMENT_SNAPSHOT_OPERATION = "materialize_statement_snapshot"
STATEMENT_SNAPSHOT_RUNTIME = "runtime-profile:dalton-core-local-decimal:0.1"
STATEMENT_SNAPSHOT_PERMISSION = "read_exact_sec_company_facts_artifact"
STATEMENT_SNAPSHOT_OUTPUT_CONTRACT = "schema:statement-snapshot-probe-output:0.1"
STATEMENT_SNAPSHOT_VERIFIER = "verifier:statement-snapshot-decimal-tie-out:0.1"

_SCHEMA_PATH = Path(__file__).with_name("statement_snapshot_schema.sql")
_HUMAN_RE = re.compile(r"^human:[A-Za-z0-9][A-Za-z0-9._/@:-]*$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ACCESSION_RE = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
_CONCEPT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]*$")
_LINE_ITEM_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_DECIMAL_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_NONNEGATIVE_DECIMAL_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_FORMS = frozenset({"10-Q", "10-K"})


class StatementSnapshotError(RuntimeError):
    pass


class StatementSnapshotValidationError(StatementSnapshotError, ValueError):
    pass


class StatementSnapshotConflict(StatementSnapshotError):
    pass


class StatementSnapshotNotFound(StatementSnapshotError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StatementSnapshotValidationError(f"{name} must be non-empty text")
    return value.strip()


def _human(value: Any, name: str = "actor_ref") -> str:
    value = _text(value, name)
    if _HUMAN_RE.fullmatch(value) is None:
        raise StatementSnapshotValidationError(f"{name} must use the human: namespace")
    return value


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if _HASH_RE.fullmatch(value) is None:
        raise StatementSnapshotValidationError(f"{name} must be lowercase SHA-256")
    return value


def _iso_date(value: Any, name: str) -> str:
    value = _text(value, name)
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise StatementSnapshotValidationError(f"{name} must be YYYY-MM-DD") from exc
    return value


def _accession(value: Any, name: str = "accession") -> str:
    value = _text(value, name)
    if _ACCESSION_RE.fullmatch(value) is None:
        raise StatementSnapshotValidationError(f"{name} must be a canonical SEC accession")
    return value


def _cik(value: Any, name: str = "issuer_cik") -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise StatementSnapshotValidationError(f"{name} must be a CIK")
    raw = str(value).strip()
    if not raw.isdigit() or len(raw) > 10:
        raise StatementSnapshotValidationError(f"{name} must contain at most ten digits")
    return raw.zfill(10)


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise StatementSnapshotValidationError(f"{name} must be an object")
    wire = dict(value)
    if set(wire) != fields:
        raise StatementSnapshotValidationError(
            f"{name} has invalid closed shape; "
            f"missing={sorted(fields - set(wire))}, unknown={sorted(set(wire) - fields)}"
        )
    return wire


def _unique_texts(value: Any, name: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise StatementSnapshotValidationError(f"{name} must be an array")
    result = [_text(item, f"{name}[]") for item in value]
    if nonempty and not result:
        raise StatementSnapshotValidationError(f"{name} must not be empty")
    if len(result) != len(set(result)):
        raise StatementSnapshotValidationError(f"{name} must contain unique values")
    return result


def _decimal_string(value: Any, name: str, *, nonnegative: bool = False) -> str:
    pattern = _NONNEGATIVE_DECIMAL_RE if nonnegative else _DECIMAL_RE
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise StatementSnapshotValidationError(f"{name} must be a canonical decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise StatementSnapshotValidationError(f"{name} must be Decimal-compatible") from exc
    if not parsed.is_finite():
        raise StatementSnapshotValidationError(f"{name} must be finite")
    return value


def _fact_decimal(value: Any, name: str) -> tuple[Decimal, str]:
    if isinstance(value, bool) or isinstance(value, float):
        raise StatementSnapshotValidationError(
            f"{name} must not use boolean or binary floating-point input"
        )
    if isinstance(value, int):
        raw = str(value)
    elif isinstance(value, str):
        raw = _decimal_string(value, name)
    else:
        raise StatementSnapshotValidationError(f"{name} must be an integer or decimal string")
    parsed = Decimal(raw)
    return parsed, _format_decimal(parsed)


def _format_decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    raw = format(value, "f")
    if "." in raw:
        raw = raw.rstrip("0").rstrip(".")
    return raw


def _record(base: Mapping[str, Any]) -> dict[str, Any]:
    wire = dict(base)
    wire["content_hash"] = content_hash(wire)
    return wire


def _ref(prefix: str, identity: Mapping[str, Any]) -> str:
    return f"{prefix}:{content_hash(identity)[:32]}"


def _decode(row: sqlite3.Row | None, name: str) -> dict[str, Any]:
    if row is None:
        raise StatementSnapshotNotFound(name)
    try:
        wire = json.loads(row["record_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise StatementSnapshotConflict(f"{name} record_json is invalid") from exc
    if not isinstance(wire, dict) or canonical_json(wire) != row["record_json"]:
        raise StatementSnapshotConflict(f"{name} record_json is not canonical")
    body = dict(wire)
    asserted = body.pop("content_hash", None)
    if asserted != content_hash(body) or asserted != row["content_hash"]:
        raise StatementSnapshotConflict(f"{name} content hash drifted")
    version_column = row["version_id"]
    if wire.get("id") != version_column:
        raise StatementSnapshotConflict(f"{name} identity column drifted")
    checks = {
        "version_number": wire.get("version"),
        "prior_version_id": wire.get("prior_version_ref"),
        "actor_ref": wire.get("actor_ref"),
        "created_at": wire.get("created_at"),
    }
    for column, expected in checks.items():
        if column in row.keys() and row[column] != expected:
            raise StatementSnapshotConflict(f"{name} {column} drifted")
    return wire


def _concept_set_shape(
    line_items: Sequence[Mapping[str, Any]], equations: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(line_items, (list, tuple)) or len(line_items) < 3:
        raise StatementSnapshotValidationError("line_items must contain at least three rows")
    normalized_items: list[dict[str, Any]] = []
    item_refs: set[str] = set()
    concept_owner: dict[str, str] = {}
    for index, raw in enumerate(line_items):
        item = _closed(raw, {"line_item_ref", "concepts"}, f"line_items[{index}]")
        line_item_ref = _text(item["line_item_ref"], f"line_items[{index}].line_item_ref")
        if _LINE_ITEM_RE.fullmatch(line_item_ref) is None or line_item_ref in item_refs:
            raise StatementSnapshotValidationError("line_item_ref is invalid or duplicated")
        concepts = _unique_texts(item["concepts"], f"line_items[{index}].concepts", nonempty=True)
        if len(concepts) > 8 or any(_CONCEPT_RE.fullmatch(tag) is None for tag in concepts):
            raise StatementSnapshotValidationError("concept allowlist is invalid")
        for tag in concepts:
            if tag in concept_owner:
                raise StatementSnapshotValidationError(
                    f"concept {tag} maps to more than one line item"
                )
            concept_owner[tag] = line_item_ref
        item_refs.add(line_item_ref)
        normalized_items.append({"line_item_ref": line_item_ref, "concepts": concepts})
    if not isinstance(equations, (list, tuple)) or not equations:
        raise StatementSnapshotValidationError("equations must not be empty")
    normalized_equations: list[dict[str, Any]] = []
    equation_refs: set[str] = set()
    for index, raw in enumerate(equations):
        equation = _closed(
            raw,
            {"equation_ref", "left_line_items", "right_line_items", "tolerance"},
            f"equations[{index}]",
        )
        equation_ref = _text(equation["equation_ref"], f"equations[{index}].equation_ref")
        if _LINE_ITEM_RE.fullmatch(equation_ref) is None or equation_ref in equation_refs:
            raise StatementSnapshotValidationError("equation_ref is invalid or duplicated")
        left = _unique_texts(
            equation["left_line_items"], f"equations[{index}].left_line_items", nonempty=True
        )
        right = _unique_texts(
            equation["right_line_items"], f"equations[{index}].right_line_items", nonempty=True
        )
        if not set(left + right).issubset(item_refs):
            raise StatementSnapshotValidationError("equation references an unknown line item")
        if set(left) & set(right):
            raise StatementSnapshotValidationError("equation sides must not overlap")
        equation_refs.add(equation_ref)
        normalized_equations.append({
            "equation_ref": equation_ref,
            "left_line_items": left,
            "right_line_items": right,
            "tolerance": _decimal_string(
                equation["tolerance"], f"equations[{index}].tolerance", nonnegative=True
            ),
        })
    return normalized_items, normalized_equations


class StatementSnapshotAuthority:
    """Append-only concept-set and verified statement-snapshot authority."""

    def __init__(
        self,
        store: Any,
        *,
        artifact_resolver: Callable[[str], Mapping[str, Any]],
    ):
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("StatementSnapshotAuthority requires a DaltonStore")
        if not callable(artifact_resolver):
            raise TypeError("StatementSnapshotAuthority requires an exact artifact resolver")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.artifact_resolver = artifact_resolver
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    def concept_set(self, version_ref: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM statement_concept_set_versions WHERE version_id=?",
            (_text(version_ref, "concept_set_version_ref"),),
        ).fetchone()
        return _decode(row, f"StatementConceptSetVersion {version_ref}")

    def snapshot(self, version_ref: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM statement_snapshot_versions WHERE version_id=?",
            (_text(version_ref, "statement_snapshot_version_ref"),),
        ).fetchone()
        return _decode(row, f"StatementSnapshotVersion {version_ref}")

    def publish_concept_set(
        self,
        concept_set_ref: str,
        *,
        line_items: Sequence[Mapping[str, Any]],
        equations: Sequence[Mapping[str, Any]],
        actor_ref: str,
        prior_version_ref: str | None = None,
    ) -> dict[str, Any]:
        concept_set_ref = _text(concept_set_ref, "concept_set_ref")
        actor_ref = _human(actor_ref)
        normalized_items, normalized_equations = _concept_set_shape(line_items, equations)
        latest = self.connection.execute(
            "SELECT * FROM statement_concept_set_versions WHERE concept_set_ref=? "
            "ORDER BY version_number DESC LIMIT 1",
            (concept_set_ref,),
        ).fetchone()
        if latest is None:
            if prior_version_ref is not None:
                raise StatementSnapshotConflict("first concept set cannot have a prior version")
            version = 1
        else:
            latest_wire = _decode(latest, "latest StatementConceptSetVersion")
            if (
                prior_version_ref is None
                and latest_wire["line_items"] == normalized_items
                and latest_wire["equations"] == normalized_equations
            ):
                return {"status": "duplicate", **latest_wire}
            if prior_version_ref != latest_wire["id"]:
                raise StatementSnapshotConflict("concept set must continue the latest version")
            version = latest_wire["version"] + 1
        identity = {
            "concept_set_ref": concept_set_ref,
            "version": version,
            "prior_version_ref": prior_version_ref,
            "statement_type": "balance_sheet",
            "taxonomy": "us-gaap",
            "unit": "USD",
            "line_items": normalized_items,
            "equations": normalized_equations,
        }
        version_id = _ref("statement-concept-set-version", identity)
        wire = _record({
            "schema_version": SCHEMA_VERSION,
            "id": version_id,
            "created_at": _now(),
            **identity,
            "actor_ref": actor_ref,
        })
        with self.store._transaction() as cur:
            cur.execute(
                "INSERT INTO statement_concept_set_versions "
                "(version_id,concept_set_ref,version_number,prior_version_id,statement_type,"
                "taxonomy,unit,record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    version_id, concept_set_ref, version, prior_version_ref, "balance_sheet",
                    "us-gaap", "USD", canonical_json(wire), wire["content_hash"],
                    actor_ref, wire["created_at"],
                ),
            )
        return {"status": "fresh", **wire}

    @staticmethod
    def _extract_rows(
        raw_payload: bytes,
        concept_set: Mapping[str, Any],
        *,
        source_content_hash: str,
        issuer_cik: str,
        accession: str,
        form: str,
        period_end: str,
    ) -> tuple[str, str, list[dict[str, Any]]]:
        if not isinstance(raw_payload, bytes):
            raise StatementSnapshotValidationError("raw_payload must be exact artifact bytes")
        if hashlib.sha256(raw_payload).hexdigest() != source_content_hash:
            raise StatementSnapshotConflict("source artifact byte hash does not match authority")
        try:
            payload = json.loads(raw_payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StatementSnapshotValidationError("source artifact is not valid JSON") from exc
        if not isinstance(payload, Mapping):
            raise StatementSnapshotValidationError("SEC Company Facts payload must be an object")
        if _cik(payload.get("cik"), "payload.cik") != issuer_cik:
            raise StatementSnapshotConflict("SEC Company Facts CIK does not match the request")
        entity_name = _text(payload.get("entityName"), "payload.entityName")
        facts = payload.get("facts")
        taxonomy_facts = facts.get(concept_set["taxonomy"]) if isinstance(facts, Mapping) else None
        if not isinstance(taxonomy_facts, Mapping):
            raise StatementSnapshotValidationError("SEC Company Facts lacks the concept-set taxonomy")
        rows: list[dict[str, Any]] = []
        for item in concept_set["line_items"]:
            selected: dict[str, Any] | None = None
            for tag in item["concepts"]:
                concept = taxonomy_facts.get(tag)
                if concept is None:
                    continue
                if not isinstance(concept, Mapping):
                    raise StatementSnapshotValidationError("SEC concept must be an object")
                label = _text(concept.get("label"), f"concept {tag}.label")
                units = concept.get("units")
                values = units.get(concept_set["unit"]) if isinstance(units, Mapping) else None
                if not isinstance(values, list):
                    continue
                eligible: list[dict[str, Any]] = []
                for fact in values:
                    if not isinstance(fact, Mapping):
                        raise StatementSnapshotValidationError("SEC fact must be an object")
                    if (
                        fact.get("accn") != accession
                        or fact.get("form") != form
                        or fact.get("end") != period_end
                        or fact.get("start") is not None
                    ):
                        continue
                    _, value = _fact_decimal(fact.get("val"), f"{tag}.val")
                    filed = _iso_date(fact.get("filed"), f"{tag}.filed")
                    fy = fact.get("fy")
                    if isinstance(fy, bool) or not isinstance(fy, int) or not 1900 <= fy <= 9999:
                        raise StatementSnapshotValidationError(f"{tag}.fy must be a fiscal year")
                    fp = _text(fact.get("fp"), f"{tag}.fp")
                    eligible.append({
                        "line_item_ref": item["line_item_ref"],
                        "taxonomy": concept_set["taxonomy"],
                        "concept": tag,
                        "label": label,
                        "unit": concept_set["unit"],
                        "value": value,
                        "period_end": period_end,
                        "accession": accession,
                        "form": form,
                        "filed": filed,
                        "fy": fy,
                        "fp": fp,
                    })
                unique = {canonical_json(row): row for row in eligible}
                if len(unique) > 1:
                    raise StatementSnapshotConflict(
                        f"concept {tag} has ambiguous facts on the exact accession"
                    )
                if unique:
                    selected = next(iter(unique.values()))
                    break
            if selected is None:
                raise StatementSnapshotValidationError(
                    f"no human-admitted concept resolves for {item['line_item_ref']}"
                )
            rows.append(selected)
        filed_dates = {row["filed"] for row in rows}
        if len(filed_dates) != 1:
            raise StatementSnapshotConflict("exact-accession statement facts disagree on filed date")
        return entity_name, next(iter(filed_dates)), rows

    @staticmethod
    def _reconcile(
        rows: Sequence[Mapping[str, Any]], equations: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        values = {row["line_item_ref"]: Decimal(row["value"]) for row in rows}
        reconciliations: list[dict[str, Any]] = []
        for equation in equations:
            left = sum((values[item] for item in equation["left_line_items"]), Decimal(0))
            right = sum((values[item] for item in equation["right_line_items"]), Decimal(0))
            difference = left - right
            tolerance = Decimal(equation["tolerance"])
            if abs(difference) > tolerance:
                raise StatementSnapshotConflict(
                    f"statement equation {equation['equation_ref']} failed Decimal reconciliation"
                )
            reconciliations.append({
                "equation_ref": equation["equation_ref"],
                "left_value": _format_decimal(left),
                "right_value": _format_decimal(right),
                "difference": _format_decimal(difference),
                "tolerance": equation["tolerance"],
                "status": "passed",
            })
        return reconciliations

    def materialize_snapshot(
        self,
        *,
        raw_payload: bytes,
        source_artifact_version_ref: str,
        source_artifact_version_hash: str,
        source_content_hash: str,
        concept_set_version_ref: str,
        concept_set_version_hash: str,
        issuer_cik: str,
        accession: str,
        form: str,
        period_end: str,
        prior_version_ref: str | None = None,
    ) -> dict[str, Any]:
        source_artifact_version_ref = _text(
            source_artifact_version_ref, "source_artifact_version_ref"
        )
        source_artifact_version_hash = _hash(
            source_artifact_version_hash, "source_artifact_version_hash"
        )
        source_content_hash = _hash(source_content_hash, "source_content_hash")
        try:
            source_artifact = dict(self.artifact_resolver(source_artifact_version_ref))
        except Exception as exc:
            raise StatementSnapshotNotFound(
                f"source ArtifactVersion {source_artifact_version_ref} is unavailable"
            ) from exc
        required_artifact_fields = {
            "schema_version", "id", "content_hash", "artifact_content_hash",
            "size_bytes", "kind", "media_type", "access_class",
        }
        if not required_artifact_fields.issubset(source_artifact):
            raise StatementSnapshotConflict("source ArtifactVersion is incomplete")
        artifact_body = dict(source_artifact)
        asserted_artifact_hash = artifact_body.pop("content_hash")
        if (
            source_artifact["schema_version"] not in {"0.1", "0.2"}
            or source_artifact["id"] != source_artifact_version_ref
            or asserted_artifact_hash != source_artifact_version_hash
            or content_hash(artifact_body) != asserted_artifact_hash
            or source_artifact["artifact_content_hash"] != source_content_hash
            or isinstance(source_artifact["size_bytes"], bool)
            or not isinstance(source_artifact["size_bytes"], int)
            or source_artifact["size_bytes"] != len(raw_payload)
            or source_artifact["kind"] not in {"connector_raw_response", "raw_source"}
            or source_artifact["media_type"]
            not in {"application/json", "application/sec-companyfacts+json"}
            or source_artifact["access_class"] not in {"public", "internal"}
        ):
            raise StatementSnapshotConflict("source ArtifactVersion binding failed")
        issuer_cik = _cik(issuer_cik)
        accession = _accession(accession)
        form = _text(form, "form")
        if form not in _FORMS:
            raise StatementSnapshotValidationError("form must be 10-Q or 10-K")
        period_end = _iso_date(period_end, "period_end")
        concept_set = self.concept_set(concept_set_version_ref)
        if concept_set["content_hash"] != _hash(
            concept_set_version_hash, "concept_set_version_hash"
        ):
            raise StatementSnapshotConflict("concept-set hash binding failed")
        entity_name, filed, rows = self._extract_rows(
            raw_payload,
            concept_set,
            source_content_hash=source_content_hash,
            issuer_cik=issuer_cik,
            accession=accession,
            form=form,
            period_end=period_end,
        )
        reconciliations = self._reconcile(rows, concept_set["equations"])
        snapshot_ref = f"statement-snapshot:{issuer_cik}:{accession}:balance-sheet"
        latest = self.connection.execute(
            "SELECT * FROM statement_snapshot_versions WHERE snapshot_ref=? "
            "ORDER BY version_number DESC LIMIT 1",
            (snapshot_ref,),
        ).fetchone()
        request_binding = {
            "source_artifact_version_ref": source_artifact_version_ref,
            "source_artifact_version_hash": source_artifact_version_hash,
            "source_content_hash": source_content_hash,
            "concept_set_version_ref": concept_set["id"],
            "concept_set_version_hash": concept_set["content_hash"],
            "issuer_cik": issuer_cik,
            "accession": accession,
            "form": form,
            "period_end": period_end,
        }
        if latest is None:
            if prior_version_ref is not None:
                raise StatementSnapshotConflict("first snapshot cannot have a prior version")
            version = 1
        else:
            latest_wire = _decode(latest, "latest StatementSnapshotVersion")
            latest_binding = {key: latest_wire[key] for key in request_binding}
            if prior_version_ref is None and latest_binding == request_binding:
                return {"status": "duplicate", **latest_wire}
            if prior_version_ref != latest_wire["id"]:
                raise StatementSnapshotConflict("snapshot must continue the latest version")
            version = latest_wire["version"] + 1
        identity = {
            "snapshot_ref": snapshot_ref,
            "version": version,
            "prior_version_ref": prior_version_ref,
            **request_binding,
        }
        version_id = _ref("statement-snapshot-version", identity)
        wire = _record({
            "schema_version": SCHEMA_VERSION,
            "id": version_id,
            "created_at": _now(),
            "snapshot_ref": snapshot_ref,
            "version": version,
            "prior_version_ref": prior_version_ref,
            "statement_type": "balance_sheet",
            "source_ref": "source:sec-edgar",
            "source_artifact_version_ref": source_artifact_version_ref,
            "source_artifact_version_hash": source_artifact_version_hash,
            "source_content_hash": source_content_hash,
            "concept_set_version_ref": concept_set["id"],
            "concept_set_version_hash": concept_set["content_hash"],
            "issuer_cik": issuer_cik,
            "entity_name": entity_name,
            "accession": accession,
            "form": form,
            "filed": filed,
            "period_end": period_end,
            "taxonomy": concept_set["taxonomy"],
            "unit": concept_set["unit"],
            "fact_rows": rows,
            "reconciliations": reconciliations,
            "verification_status": "verified",
            "actor_ref": "core:statement-snapshot-worker",
        })
        with self.store._transaction() as cur:
            cur.execute(
                "INSERT INTO statement_snapshot_versions "
                "(version_id,snapshot_ref,version_number,prior_version_id,"
                "concept_set_version_ref,source_artifact_version_ref,"
                "source_artifact_version_hash,source_content_hash,"
                "accession,issuer_cik,period_end,record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    version_id, snapshot_ref, version, prior_version_ref, concept_set["id"],
                    source_artifact_version_ref, source_artifact_version_hash,
                    source_content_hash, accession, issuer_cik, period_end,
                    canonical_json(wire), wire["content_hash"], wire["actor_ref"], wire["created_at"],
                ),
            )
        return {"status": "fresh", **wire}

    def fact_rows(
        self, snapshot_version_ref: str, line_item_refs: Sequence[str]
    ) -> list[dict[str, Any]]:
        snapshot = self.snapshot(snapshot_version_ref)
        requested = _unique_texts(line_item_refs, "line_item_refs", nonempty=True)
        by_ref = {row["line_item_ref"]: row for row in snapshot["fact_rows"]}
        unknown = [item for item in requested if item not in by_ref]
        if unknown:
            raise StatementSnapshotNotFound(
                f"snapshot does not contain line items: {', '.join(unknown)}"
            )
        return [dict(by_ref[item]) for item in requested]


class StatementSnapshotWorker:
    """Execute one already-admitted local snapshot WorkOrder."""

    _PARAMETERS = {
        "source_ref", "locator", "query_terms", "source_artifact_version_ref",
        "source_artifact_version_hash", "source_content_hash", "concept_set_version_ref",
        "concept_set_version_hash", "issuer_cik", "accession", "form",
        "period_end", "prior_snapshot_version_ref",
    }

    def __init__(self, authority: StatementSnapshotAuthority):
        self.authority = authority

    def execute(self, work_order: Mapping[str, Any], raw_payload: bytes) -> dict[str, Any]:
        work = WorkOrder.from_dict(work_order).to_dict()
        if work["requested_capabilities"] != [STATEMENT_SNAPSHOT_CAPABILITY]:
            raise StatementSnapshotConflict("WorkOrder capability is not statement-snapshot")
        if work["runtime_profile_ref"] != STATEMENT_SNAPSHOT_RUNTIME:
            raise StatementSnapshotConflict("WorkOrder runtime profile drifted")
        if work["declared_side_effects"]:
            raise StatementSnapshotConflict("statement-snapshot WorkOrder must have no external side effects")
        metadata = work["metadata"]
        if metadata.get("operation") != STATEMENT_SNAPSHOT_OPERATION:
            raise StatementSnapshotConflict("WorkOrder operation drifted")
        if metadata.get("permission_scope") != STATEMENT_SNAPSHOT_PERMISSION:
            raise StatementSnapshotConflict("WorkOrder permission scope drifted")
        parameters = _closed(metadata.get("parameters"), self._PARAMETERS, "parameters")
        concept_set = self.authority.concept_set(
            _text(parameters["concept_set_version_ref"], "concept_set_version_ref")
        )
        if parameters["concept_set_version_hash"] != concept_set["content_hash"]:
            raise StatementSnapshotConflict("WorkOrder concept-set binding drifted")
        expected_terms = [item["line_item_ref"] for item in concept_set["line_items"]]
        if parameters["query_terms"] != expected_terms:
            raise StatementSnapshotConflict("WorkOrder query_terms drifted from concept set")
        accession = _accession(parameters["accession"])
        if parameters["source_ref"] != "source:sec-edgar":
            raise StatementSnapshotConflict("WorkOrder source_ref drifted")
        if parameters["locator"] != f"sec:filing:{accession}#companyfacts":
            raise StatementSnapshotConflict("WorkOrder locator drifted")
        prior = parameters["prior_snapshot_version_ref"]
        if prior is not None:
            prior = _text(prior, "prior_snapshot_version_ref")
        snapshot = self.authority.materialize_snapshot(
            raw_payload=raw_payload,
            source_artifact_version_ref=parameters["source_artifact_version_ref"],
            source_artifact_version_hash=parameters["source_artifact_version_hash"],
            source_content_hash=parameters["source_content_hash"],
            concept_set_version_ref=concept_set["id"],
            concept_set_version_hash=concept_set["content_hash"],
            issuer_cik=parameters["issuer_cik"],
            accession=accession,
            form=parameters["form"],
            period_end=parameters["period_end"],
            prior_version_ref=prior,
        )
        return {"matches": [{
            "source_location": f"sec:filing:{accession}#balance-sheet",
            "statement_snapshot_ref": snapshot["id"],
            "statement_snapshot_hash": snapshot["content_hash"],
            "concept_set_version_ref": concept_set["id"],
            "concept_set_version_hash": concept_set["content_hash"],
            "accession": accession,
            "period_end": snapshot["period_end"],
            "verification_status": snapshot["verification_status"],
        }]}


__all__ = [
    "SCHEMA_VERSION",
    "STATEMENT_SNAPSHOT_CAPABILITY",
    "STATEMENT_SNAPSHOT_OPERATION",
    "STATEMENT_SNAPSHOT_RUNTIME",
    "STATEMENT_SNAPSHOT_PERMISSION",
    "STATEMENT_SNAPSHOT_OUTPUT_CONTRACT",
    "STATEMENT_SNAPSHOT_VERIFIER",
    "StatementSnapshotAuthority",
    "StatementSnapshotWorker",
    "StatementSnapshotError",
    "StatementSnapshotValidationError",
    "StatementSnapshotConflict",
    "StatementSnapshotNotFound",
]
