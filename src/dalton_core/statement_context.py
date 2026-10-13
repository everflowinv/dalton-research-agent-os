"""Append-only complete XBRL contexts replayed from already held raw parses.

No inference, network or model: an overlay requires exact legacy bytes at
all ordinals. Where legacy normalization lost rows, a separately identified
complete snapshot is admitted against the same hash-verified source/header.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence
from contextlib import contextmanager

from .store import canonical_json, content_hash
from .store import authorization_flag, authorized_flag

NORMALIZER_REF = "rule:statement-context-replay:1"
_FIELDS = ("statement", "concept", "label", "level", "parent_concept", "is_breakdown",
           "dimension_axis", "dimension_member", "period_start", "period_end", "value", "unit", "balance")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS statement_context_versions (
    version_id TEXT PRIMARY KEY,
    ingest_id TEXT NOT NULL REFERENCES coverage_mission_statement_filings(ingest_id),
    status TEXT NOT NULL CHECK(status IN ('available','refused')),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_statement_context_ingest ON statement_context_versions(ingest_id,created_at);
CREATE TRIGGER IF NOT EXISTS statement_context_authorized_insert BEFORE INSERT ON statement_context_versions
WHEN dalton_statement_context_authorized() = 0 BEGIN
SELECT RAISE(ABORT, 'statement context insert requires StatementContextAuthority'); END;
CREATE TRIGGER IF NOT EXISTS statement_context_no_update BEFORE UPDATE ON statement_context_versions BEGIN
SELECT RAISE(ABORT, 'statement contexts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS statement_context_no_delete BEFORE DELETE ON statement_context_versions BEGIN
SELECT RAISE(ABORT, 'statement contexts are append-only'); END;
"""


class StatementContextError(ValueError):
    pass


# Exact material-request-only compatibility: no numeric parsing code changes.
# A future/unknown source SHA uses itself and cannot inherit this old identity.
_UNIT_MATERIAL_ONLY_SOURCE_ALIAS = {
    "0e77fa7a3518ff1f77221495dd92b1cc8e65b4fed23d4d75debe58bb42575073": "f45625e3e5fe70f2fc6451224fbef90727d58eac4c5cb937736b721680dd14ff",
}

# Exact performance-only compatibility. The original parser body/defaults and
# errors are byte-identical; only the pure byte-cache decorator/import changed.
# Map directly to the already effective identity, never a permissive rule.
_UNIT_PURE_PARSE_CACHE_SOURCE_ALIAS = {
    "71014cf54ab1df0c288e40f9b4dedd14d00aca4bbe76d637d32a3a5d221a423c": "6af256a98dfdfd9f63d54b15259b8a2f29784bd3ba112775faed6fe9b799a91b",
}


def normalizer_hash() -> str:
    from .sec_financials_core import sec_financials_schema_hash
    from . import sec_financials_normalise, statement_companyfacts_context, sec_filing_units
    unit_source = hashlib.sha256(Path(sec_filing_units.__file__).read_bytes()).hexdigest()
    unit_identity = _UNIT_PURE_PARSE_CACHE_SOURCE_ALIAS.get(
        unit_source, _UNIT_MATERIAL_ONLY_SOURCE_ALIAS.get(unit_source, unit_source))
    return content_hash({"rule_ref": NORMALIZER_REF,
                         "source_sha256": hashlib.sha256(Path(sec_financials_normalise.__file__).read_bytes()).hexdigest(),
                         "schema_hash": sec_financials_schema_hash(version=4),
                         "unit_rule_ref": sec_filing_units.RULE_REF,
                         "unit_source_sha256": unit_identity,
                         "supplementary_source_sha256": hashlib.sha256(Path(statement_companyfacts_context.__file__).read_bytes()).hexdigest()})


def _native_unit_normalizer_hash() -> str:
    from . import sec_native_filing_units, native_statement_membership
    return content_hash({'domestic_normalizer_hash':normalizer_hash(),
        'native_unit_contract':sec_native_filing_units.CONTRACT,
        'native_unit_source_sha256':hashlib.sha256(Path(sec_native_filing_units.__file__).read_bytes()).hexdigest(),
        'native_membership_contract':native_statement_membership.CONTRACT,
        'native_membership_source_sha256':hashlib.sha256(Path(native_statement_membership.__file__).read_bytes()).hexdigest()})


def _filing(connection: Any, ingest_id: str) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM coverage_mission_statement_filings WHERE ingest_id=?", (ingest_id,)).fetchone()
    if row is None:
        raise StatementContextError("statement context names unknown filing")
    return dict(row)


def base_statement_rows(connection: Any, ingest_id: str) -> list[dict[str, Any]]:
    from .coverage_mission import _statement_line_row
    return [_statement_line_row(row) for row in connection.execute(
        "SELECT * FROM coverage_mission_statement_lines WHERE ingest_id=? ORDER BY ordinal", (ingest_id,)).fetchall()]


def _raw_filing(state_dir: Path, filing: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
    from .acquired_sec_filings import _spool_bytes
    refs = filing.get("source_record_refs") or json.loads(filing["source_record_refs_json"])
    failures = []
    for ref in refs:
        if not isinstance(ref, str) or not ref.startswith("raw-sink:"):
            continue
        raw, problem = _spool_bytes(state_dir, ref[9:])
        if raw is None:
            failures.append(problem)
            continue
        payload = json.loads(raw)
        if not isinstance(payload, Mapping):
            raise StatementContextError("raw parse is not an object")
        if str(payload.get("cik", "")).zfill(10) != str(filing["cik"]).zfill(10):
            raise StatementContextError("raw parse CIK differs from immutable filing")
        if payload.get("entity_name") != filing["entity_name"]:
            raise StatementContextError("raw parse entity differs from immutable filing")
        matches = [item for item in payload.get("filings") or []
                   if isinstance(item, Mapping) and item.get("accession") == filing["accession"]]
        if len(matches) != 1:
            raise StatementContextError("raw parse does not uniquely identify immutable accession")
        parsed = matches[0]
        if any(parsed.get(key) != filing[key] for key in ("accession", "form", "filed", "report_date")):
            raise StatementContextError("raw parse header differs from immutable filing")
        return ref, parsed
    raise StatementContextError("; ".join(str(f) for f in failures) or "no held raw parse ref")


def prepare_context(connection: Any, state_dir: str | Path, ingest_id: str, *,
                    companyfacts_source: Mapping[str, Any] | None = None,
                    discover_companyfacts: bool = True) -> dict[str, Any]:
    """Pure read/dry-run admission; returns an available or explicit refusal record."""
    from .company_model_annual_projection import verify_statement_filing
    from .sec_financials_normalise import normalise_filing
    filing = _filing(connection, ingest_id)
    base = {"schema_version": "0.1", "ingest_id": ingest_id, "company_ref": filing["company_ref"],
            "filing_content_hash": filing["content_hash"], "normalizer_ref": NORMALIZER_REF,
            "normalizer_hash": normalizer_hash()}
    try:
        authority = verify_statement_filing(connection, filing, allow_legacy_metadata=True)
        if authority == "header_only":
            base["legacy_authority"] = "header_only"
        raw_ref, parsed = _raw_filing(Path(state_dir), filing)
        base["raw_ref"] = raw_ref
        stored = [dict(row) for row in connection.execute(
            "SELECT * FROM coverage_mission_statement_lines WHERE ingest_id=? ORDER BY ordinal", (ingest_id,)).fetchall()]
        version = int(str(filing["governance_ref"]).rsplit(":v", 1)[1])
        fields = (*_FIELDS, "dimension_count") if version >= 3 else _FIELDS
        original = [{key: (bool(row[key]) if key == "is_breakdown" else row[key]) for key in fields} for row in stored]
        matches: list[list[dict[str, Any]]] = []
        for notes, legacy_flags in ((False, False), (False, True), (True, False), (True, True)):
            replayed = normalise_filing(accession=parsed["accession"], form=parsed["form"], filed=parsed["filed"],
                                       report_date=parsed["report_date"], statements=parsed.get("statements") or {},
                                       include_note_segments=notes, legacy_breakdown_flags=legacy_flags)["lines"]
            projected = [{key: row[key] for key in fields} for row in replayed]
            if projected == original:
                matches.append(replayed)
        unique = {canonical_json([{key: row[key] for key in ("dimensions", "dimension_count")} for row in lines]): lines
                  for lines in matches}
        if len(unique) > 1:
            raise StatementContextError("legacy ordinal bytes admit conflicting complete contexts")
        if unique and authority != "header_only":
            lines = next(iter(unique.values()))
            if any(row.get("dimensions") is None for row in lines):
                raise StatementContextError("raw parser did not preserve complete dimension columns")
            mapping = {str(old["line_id"]): {"dimensions": new["dimensions"], "dimension_count": new["dimension_count"]}
                       for old, new in zip(stored, lines)}
            base.update(kind="ordinal_overlay", mapping=mapping, mapping_hash=content_hash(mapping),
                        status="available", reason=None)
        else:
            # Preserve the old authority. A complete source-derived snapshot
            # receives its own line identities, never masquerading as old rows.
            lines = normalise_filing(accession=parsed["accession"], form=parsed["form"], filed=parsed["filed"],
                                     report_date=parsed["report_date"], statements=parsed.get("statements") or {})["lines"]
            if not lines or any(row.get("dimensions") is None for row in lines):
                raise StatementContextError("legacy ordinals differ and complete source snapshot is unavailable")
            base.update(kind="complete_snapshot", lines=lines, mapping_hash=content_hash(lines), status="available",
                        reason=("historical header-only hash verified; numeric authority rebuilt from held raw" if authority == "header_only" else
                                "legacy ordinal projection differs; retained old rows and admitted independent source snapshot"))
        from .statement_companyfacts_context import supplemental_lines, validate_supplement, rebind_supplement_after_units
        if companyfacts_source is None and discover_companyfacts:
            from .valuation_history import company_facts
            companyfacts_source = company_facts(connection, state_dir, filing["company_ref"])
        original_lines = lines
        complete_lines, supplement = supplemental_lines(state_dir, filing, original_lines, companyfacts_source)
        if supplement is not None:
            base.pop("mapping", None)
            base.update(kind="complete_snapshot", lines=complete_lines, mapping_hash=content_hash(complete_lines),
                        supplementary_companyfacts=supplement,
                        reason="independent complete snapshot includes exact held companyfacts share/common-income facts")
        if supplement is not None:
            validate_supplement(base, filing)
        from .statement_original_revenue_context import supplemental_revenue_lines
        complete_lines, revenue_proof = supplemental_revenue_lines(connection, state_dir, filing, complete_lines)
        if revenue_proof is not None:
            base.pop("mapping", None)
            base.update(kind="complete_snapshot", lines=complete_lines, mapping_hash=content_hash(complete_lines),
                        supplementary_original_revenue=revenue_proof,
                        reason="independent snapshot includes exact original revenue concept/duration/dimensions")
        from .sec_filing_units import supplemental_unit_lines, validate_unit_supplement
        from .sec_native_annual import ANNUAL_FORMS
        if filing['form'] in ANNUAL_FORMS:
            from .native_statement_membership import supplement as membership, FIELD
            base['normalizer_hash'] = _native_unit_normalizer_hash()
            complete_lines, membership_proof = membership(connection, state_dir, filing, parsed, complete_lines)
            if membership_proof is not None:
                base.pop('mapping', None)
                base.update(kind='complete_snapshot', lines=complete_lines,
                            mapping_hash=content_hash(complete_lines))
                base[FIELD] = membership_proof
        from .sec_filing_units import unresolved_unit_refs
        native_units = filing['form'] in ANNUAL_FORMS and bool(unresolved_unit_refs(complete_lines))
        if native_units:
            from .sec_native_filing_units import supplemental_native_unit_lines, CONTRACT
            base.update(native_unit_contract=CONTRACT, normalizer_hash=_native_unit_normalizer_hash())
            resolved, unit_proof = supplemental_native_unit_lines(connection, state_dir, filing, complete_lines)
        else:
            resolved, unit_proof = supplemental_unit_lines(connection, state_dir, filing, complete_lines)
        if unit_proof is not None:
            base.pop("mapping", None)
            base.update(kind="complete_snapshot", lines=resolved, mapping_hash=content_hash(resolved),
                        reason="independent snapshot includes exact original XML unit/context/fact proofs")
            base['supplementary_native_units' if native_units else 'supplementary_units'] = unit_proof
            if supplement is not None:
                # Both original-source proofs must pass before composing final
                # hashes. The normalizer's companyfacts source hash versions
                # this new record; old contexts remain immutable and strict.
                if native_units:
                    from .sec_native_filing_units import validate_native_unit_supplement, rebind_companyfacts
                    validate_native_unit_supplement(connection, base, filing)
                    base['supplementary_companyfacts'] = rebind_companyfacts(
                        supplement, complete_lines, resolved, unit_proof, filing)
                else:
                    validate_unit_supplement(connection, base, filing)
                    base["supplementary_companyfacts"] = rebind_supplement_after_units(
                        supplement, complete_lines, resolved, unit_proof, filing,
                    )
        from .sec_filing_units import unresolved_unit_refs
        missing_units = unresolved_unit_refs(resolved)
        if missing_units:
            base["unresolved_unit_refs"] = missing_units
    except (ValueError, KeyError, TypeError, OSError) as exc:
        base.update(kind="refusal", status="refused", reason=str(exc), mapping_hash=None)
    base["version_id"] = "statement-context:" + content_hash(base)[:32]
    base["content_hash"] = content_hash(base)
    return base


def context_record(connection: Any, version_id: str) -> dict[str, Any]:
    from .company_model_annual_projection import verify_statement_filing
    row = connection.execute("SELECT * FROM statement_context_versions WHERE version_id=?", (version_id,)).fetchone()
    if row is None:
        raise StatementContextError("statement context version is unavailable")
    record = json.loads(row["record_json"])
    base = dict(record)
    asserted = base.pop("content_hash", None)
    if canonical_json(record) != row["record_json"] or asserted != content_hash(base) or asserted != row["content_hash"]:
        raise StatementContextError("statement context record hash drifted")
    identity = {key: value for key, value in record.items() if key not in {"version_id", "content_hash"}}
    if (record.get("version_id") != version_id or version_id != "statement-context:" + content_hash(identity)[:32]
            or record.get("ingest_id") != row["ingest_id"] or record.get("status") != "available"):
        raise StatementContextError("statement context version identity or status is invalid")
    filing = _filing(connection, record["ingest_id"])
    authority = verify_statement_filing(connection, filing, allow_legacy_metadata=True)
    if authority == "header_only" and (record.get("legacy_authority") != "header_only"
                                       or record.get("kind") != "complete_snapshot"):
        raise StatementContextError("header-only filing requires independent numeric snapshot")
    refs = json.loads(filing["source_record_refs_json"])
    if (record.get("filing_content_hash") != filing["content_hash"] or record.get("company_ref") != filing["company_ref"]
            or record.get("raw_ref") not in refs or record.get("normalizer_ref") != NORMALIZER_REF):
        raise StatementContextError("statement context filing/source binding drifted")
    material = record.get("mapping") if record.get("kind") == "ordinal_overlay" else record.get("lines")
    if content_hash(material) != record.get("mapping_hash"):
        raise StatementContextError("statement context mapping hash drifted")
    from .statement_companyfacts_context import validate_supplement
    validate_supplement(record, filing)
    from .sec_filing_units import validate_unit_supplement
    validate_unit_supplement(connection, record, filing)
    from .sec_native_filing_units import validate_native_unit_supplement
    validate_native_unit_supplement(connection, record, filing)
    from .statement_original_revenue_context import validate_revenue_supplement
    validate_revenue_supplement(connection, record, filing)
    from .native_statement_membership import validate as validate_membership
    validate_membership(connection, record, filing)
    if "unresolved_unit_refs" in record:
        from .sec_filing_units import unresolved_unit_refs
        source_rows = record.get("lines") if record.get("kind") == "complete_snapshot" else base_statement_rows(connection, record["ingest_id"])
        if record["unresolved_unit_refs"] != unresolved_unit_refs(source_rows):
            raise StatementContextError("unresolved unit gap differs from exact statement rows")
    return record


def _latest(connection: Any, ingest_id: str) -> str | None:
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='statement_context_versions'").fetchone() is None:
        return None
    row = connection.execute("SELECT version_id FROM statement_context_versions WHERE ingest_id=? AND status='available' "
                             "ORDER BY created_at DESC,version_id DESC LIMIT 1", (ingest_id,)).fetchone()
    return None if row is None else str(row[0])


def statement_rows(connection: Any, ingest_id: str, *, version_id: str | None = None,
                   latest: bool = True) -> list[dict[str, Any]]:
    """One exact context snapshot; latest=False with no version is the old shape."""
    version_id = version_id or (_latest(connection, ingest_id) if latest else None)
    base = base_statement_rows(connection, ingest_id)
    if version_id is None:
        return base
    record = context_record(connection, version_id)
    if record["ingest_id"] != ingest_id:
        raise StatementContextError("context version belongs to another ingest")
    if record["kind"] == "ordinal_overlay":
        mapping = record["mapping"]
        if set(mapping) != {row["line_id"] for row in base}:
            raise StatementContextError("context mapping does not cover exact legacy line ids")
        return [{**row, **mapping[row["line_id"]], "statement_context_ref": version_id} for row in base]
    if record["kind"] == "complete_snapshot":
        return [{**row, "ingest_id": ingest_id, "ordinal": ordinal,
                 "line_id": f"{ingest_id}#{version_id}#{ordinal}", "statement_context_ref": version_id}
                for ordinal, row in enumerate(record["lines"])]
    raise StatementContextError("unsupported statement context kind")


def resolve_line_refs(connection: Any, refs: Sequence[str], bindings: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Resolve filing-proof refs through its exact recorded context versions."""
    bindings = dict(bindings or {})
    ingests = set(bindings)
    if refs:
        placeholders = ",".join("?" for _ in refs)
        ingests.update(str(row[0]) for row in connection.execute(
            "SELECT DISTINCT ingest_id FROM coverage_mission_statement_lines WHERE line_id IN (" + placeholders + ")", list(refs)).fetchall())
    resolved = {row["line_id"]: row for ingest in ingests
                for row in statement_rows(connection, ingest, version_id=bindings.get(ingest), latest=False)}
    missing = set(refs) - set(resolved)
    if missing:
        raise StatementContextError("filing proof names unavailable exact statement context lines")
    return [resolved[ref] for ref in refs]


class StatementContextAuthority:
    _authorized = authorized_flag()

    def __init__(self, store: Any):
        self.store, self.connection = store, store.connection
        self._authorization_flag = authorization_flag(self.connection, "dalton_statement_context_authorized")
        self.connection.executescript(_SCHEMA)

    @contextmanager
    def _transaction(self):
        self._authorized = True
        try:
            with self.store._transaction() as cur:
                yield cur
        finally:
            self._authorized = False

    def backfill(self, state_dir: str | Path, ingest_id: str, *,
                 companyfacts_source: Mapping[str, Any] | None = None,
                 discover_companyfacts: bool = True) -> dict[str, Any]:
        from datetime import datetime, timezone
        record = prepare_context(self.connection, state_dir, ingest_id, companyfacts_source=companyfacts_source, discover_companyfacts=discover_companyfacts)
        with self._transaction() as cur:
            cur.execute("INSERT OR IGNORE INTO statement_context_versions(version_id,ingest_id,status,record_json,content_hash,created_at) "
                        "VALUES(?,?,?,?,?,?)", (record["version_id"], ingest_id, record["status"], canonical_json(record),
                                                record["content_hash"], datetime.now(timezone.utc).isoformat()))
        from .sec_native_filing_units import open_unit_gap_request
        open_unit_gap_request(state_dir, _filing(self.connection, ingest_id), record)
        return record


def refresh_company_contexts(store: Any, state_dir: str | Path, company_ref: str, *,
                             ingest_ids: Sequence[str] | None = None, force: bool = False) -> list[dict[str, Any]]:
    """Append exact held-source contexts when either statements or facts arrive.

    No network or model call is issued. An unresolved exact XML unit gap
    opens an idempotent material request. A previously admitted version with
    the same held companyfacts hash and normalizer contract needs no new replay
    unless an acquired primary document explicitly forces refresh.
    """
    from .valuation_history import company_facts
    connection = store.connection
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                          "AND name='coverage_mission_statement_filings'").fetchone() is None:
        return []
    filings = connection.execute("SELECT ingest_id FROM coverage_mission_statement_filings "
                                 "WHERE company_ref=? ORDER BY filed,ingest_id", (company_ref,)).fetchall()
    selected = None if ingest_ids is None else set(ingest_ids)
    source = company_facts(connection, state_dir, company_ref)
    authority = StatementContextAuthority(store)
    contract_hash = normalizer_hash()
    results = []
    for filing in filings:
        ingest_id = filing["ingest_id"]
        if selected is not None and ingest_id not in selected:
            continue
        version_id = _latest(connection, ingest_id)
        if version_id:
            previous = context_record(connection, version_id)
            supplement = previous.get("supplementary_companyfacts") or {}
            from .sec_native_annual import ANNUAL_FORMS
            from .sec_filing_units import unresolved_unit_refs
            native = _filing(connection, ingest_id)['form'] in ANNUAL_FORMS
            expected_hash = (_native_unit_normalizer_hash() if native
                             else contract_hash)
            if (not force and previous.get("normalizer_hash") == expected_hash
                    and ((source is None and not supplement)
                         or (source is not None and supplement.get("raw_hash") == source["raw_hash"]))):
                results.append({"ingest_id": ingest_id, "version_id": version_id,
                                "status": "available", "reused": True})
                continue
        record = authority.backfill(state_dir, ingest_id, companyfacts_source=source,
                                    discover_companyfacts=False)
        results.append({"ingest_id": ingest_id, "version_id": record["version_id"],
                        "status": record["status"], "reason": record.get("reason"), "reused": False})
    return results
