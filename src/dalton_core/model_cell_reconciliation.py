"""Immutable per-cell forecast/actual comparison; never revises a model."""
from __future__ import annotations
import json
from decimal import Decimal
from typing import Any, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from .store import canonical_json, content_hash, authorization_flag, authorized_flag

RULE_REF = "forecast-model-cell-reconciliation:1"
ROLE_CONCEPTS = {"revenue": ("us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "us-gaap:Revenues", "us-gaap:SalesRevenueNet"),
                 "operating_income": ("us-gaap:OperatingIncomeLoss",), "net_income": ("us-gaap:NetIncomeLoss",),
                 "eps": ("us-gaap:EarningsPerShareDiluted", "us-gaap:EarningsPerShareBasicAndDiluted"),
                 "diluted_shares": ("us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",)}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS forecast_model_cell_reconciliations (
 reconciliation_id TEXT PRIMARY KEY, company_ref TEXT NOT NULL, model_version_ref TEXT NOT NULL,
 cell_ref TEXT NOT NULL, period_end TEXT NOT NULL, status TEXT NOT NULL,
 record_json TEXT NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_model_cell_reconciliation_company
ON forecast_model_cell_reconciliations(company_ref,period_end,created_at);
CREATE TRIGGER IF NOT EXISTS forecast_model_cell_reconciliations_authorized_insert
BEFORE INSERT ON forecast_model_cell_reconciliations WHEN dalton_model_cell_reconciliation_authorized()=0
BEGIN SELECT RAISE(ABORT,'model cell reconciliation requires authority'); END;
CREATE TRIGGER IF NOT EXISTS forecast_model_cell_reconciliations_no_update BEFORE UPDATE ON forecast_model_cell_reconciliations
BEGIN SELECT RAISE(ABORT,'model cell reconciliations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS forecast_model_cell_reconciliations_no_delete BEFORE DELETE ON forecast_model_cell_reconciliations
BEGIN SELECT RAISE(ABORT,'model cell reconciliations are immutable'); END;
"""


def _exists(connection: Any, table: str) -> bool:
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _verified(row: Any) -> dict[str, Any]:
    record = json.loads(row["record_json"])
    body = dict(record)
    digest = body.pop("content_hash", None)
    if digest != row["content_hash"] or digest != content_hash(body):
        raise ValueError("model reconciliation source hash drifted")
    return record


def collect_actuals(connection: Any, company_ref: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Exact complete identities, with declared additive quarter derivations."""
    from .statement_context import statement_rows
    from .company_model_annual_projection import verify_statement_filing
    from .company_model_series import quarterly_series
    from .company_model_inputs import _model_unit
    grouped = {}; events = []
    if _exists(connection, "coverage_mission_statement_filings"):
        for row in connection.execute("SELECT * FROM coverage_mission_statement_filings WHERE company_ref=? ORDER BY filed", (company_ref,)):
            filing = dict(row)
            # Header-only historical contracts can prove a publication date,
            # but their old numeric rows do not become actual-value authority.
            metadata_status = verify_statement_filing(connection, filing, allow_legacy_metadata=True)
            rows = statement_rows(connection, filing["ingest_id"])
            if metadata_status == "header_only" and (not rows or not rows[0].get("statement_context_ref")):
                rows = []
            elif not rows or not rows[0].get("statement_context_ref"):
                verify_statement_filing(connection, filing)
            events.append({"period_end": filing["report_date"], "available_on": filing["filed"], "source_ref": filing["ingest_id"], "source_hash": filing["content_hash"]})
            for line in rows:
                # Missing complete identity cannot certify a dimension slice.
                dims = line.get("dimensions")
                if dims is None and (line.get("dimension_axis") or line.get("dimension_count")):
                    continue
                dims = dims or {}
                key = (line["statement"], line["concept"], canonical_json(dims))
                grouped.setdefault(key, []).append({**line, "dimensions": dims, "accession": filing["accession"],
                                                    "filed": filing["filed"], "form": filing["form"]})
    actuals = []
    for (statement, concept, dims), rows in grouped.items():
        series = quarterly_series(rows)
        for cell in [*series["quarters"], *series["instants"]]:
            source = [row for row in rows if row["accession"] in cell.get("source_accessions", [])
                      and row["period_end"] <= cell["period_end"]]
            refs = [{key: row[key] for key in ("line_id", "ingest_id", "statement_context_ref", "accession", "filed") if key in row} for row in source]
            if not source:
                continue
            actuals.append({"statement": statement, "concept": concept, "dimensions": json.loads(dims),
                            "period_start": cell.get("period_start"), "period_end": cell["period_end"],
                            "value": cell["value"], "unit": _model_unit(cell["unit"]), "basis": "filed",
                            "available_on": max(row["filed"] for row in source), "refs": refs,
                            "derivation": cell["basis"], "source_hash": content_hash({"rows": source, "cell": cell})})
    if _exists(connection, "preliminary_actuals"):
        from .earnings_release_intake import SCALES
        for row in connection.execute("SELECT * FROM preliminary_actuals WHERE company_ref=? ORDER BY created_at", (company_ref,)):
            record = _verified(row)
            read_row = connection.execute("SELECT * FROM earnings_release_reads WHERE read_id=?", (record["read_ref"],)).fetchone()
            read = _verified(read_row)
            if read["status"] != "parsed" or record["body_sha256"] != read["body_sha256"]:
                raise ValueError("preliminary source parse binding differs")
            event = {"period_end": record["period_end"], "available_on": str(record["report_date"]), "source_ref": record["read_ref"], "source_hash": read["content_hash"]}
            if event not in events: events.append(event)
            concepts = ROLE_CONCEPTS.get(record["metric"], ())
            if not concepts: continue
            actuals.append({"statement": "income", "concept": None, "role": record["metric"], "dimensions": {},
                            "period_start": record["period_start"], "period_end": record["period_end"],
                            "value": format(Decimal(record["value"])*SCALES[record["scale"]], "f"),
                            "unit": _model_unit(record["unit"]), "basis": "preliminary", "available_on": event["available_on"],
                            "refs": [{"ref": record["id"], "source_hash": record["content_hash"], "body_sha256": record["body_sha256"], "span": record["span"]}],
                            "source_hash": record["content_hash"]})
    return actuals, events


def compare_cells(model: Mapping[str, Any], actuals: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Same period, unit and explicit identity; an absence is its own outcome."""
    lines = {line["ref"]: line for line in (model.get("financial_statement_structure") or {}).get("lines") or []}
    drivers = {driver["ref"]: driver for driver in model.get("drivers") or []}
    assumptions = {a["ref"]: a for a in model.get("assumptions") or []}
    ends = {period["end"] for period in model.get("forecast_periods") or []}
    output = []
    for result in model.get("results") or []:
        line = lines.get(str(result["ref"]).removeprefix("result:"), {})
        driver = drivers.get(result.get("driver_ref"), {})
        identity = driver.get("statement_identity") or line.get("statement_identity") or {"statement": line.get("statement", "income"), "dimensions": {}}
        concept = line.get("concept")
        concepts = (concept,) if concept else ROLE_CONCEPTS.get(result.get("role"), ())
        for cell in result.get("cells") or []:
            period = cell["period"]
            if cell.get("kind") != "estimate" or period["end"] not in ends or not any(event["period_end"] == period["end"] for event in events):
                continue
            candidates = [actual for actual in actuals if actual["period_end"] == period["end"]
                          and actual.get("period_start") in {None, period["start"]}
                          and actual["statement"] == identity["statement"] and actual["dimensions"] == identity["dimensions"]
                          and actual["unit"].lower() == result["unit"].lower()
                          and (actual.get("concept") in concepts or (actual["basis"] == "preliminary" and not identity["dimensions"] and actual.get("role") == result.get("role")))]
            filed = [actual for actual in candidates if actual["basis"] == "filed"]
            candidates = filed or candidates
            candidates.sort(key=lambda actual: (actual["available_on"], actual["source_hash"]), reverse=True)
            picked = candidates[0] if candidates else None
            period_events = [event for event in events if event["period_end"] == period["end"]]
            first_publication = min([event["available_on"][:10] for event in period_events]
                                    + ([picked["available_on"][:10]] if picked is not None else []))
            reason = None
            if not concepts: reason = "no_declared_comparable_filed_concept"
            elif picked is None: reason = "exact_period_identity_unit_actual_missing"
            elif str(model["created_at"])[:10] >= first_publication: reason = "forecast_not_known_before_actual_publication"
            elif len({actual["value"] for actual in candidates if actual["available_on"] == picked["available_on"]}) > 1: reason = "conflicting_exact_actual_sources"
            elif cell.get("value") is None or cell.get("status") != "computed": reason = "forecast_cell_unavailable"
            bound_assumptions = [assumptions[ref] for ref in cell.get("assumption_refs") or [] if ref in assumptions]
            body = {"rule_ref": RULE_REF, "company_ref": model["company_ref"], "model_version_ref": model["id"], "model_hash": model["content_hash"],
                    "cell_ref": cell["ref"], "result_ref": result["ref"], "period": period, "unit": result["unit"],
                    "forecast": cell.get("value"), "actual": picked, "status": "unavailable" if reason else "compared", "reason": reason,
                    "actual_event_refs": period_events, "first_actual_available_on": first_publication,
                    "assumption_refs": cell.get("assumption_refs") or [], "method_refs": sorted({str(a.get("provenance", {}).get("rule_ref")) for a in bound_assumptions if a.get("provenance", {}).get("rule_ref")}),
                    "attribution": "bound_assumptions_and_methods; causal error attribution requires review", "redraft_required": False}
            if not reason:
                difference = Decimal(picked["value"])-Decimal(cell["value"])
                percentage = None if Decimal(cell["value"]) == 0 else difference/abs(Decimal(cell["value"]))*100
                magnitude = abs(percentage) if percentage is not None else None
                band = "zero_forecast_absolute_only" if magnitude is None else "overturn_candidate" if magnitude >= 3 else "notable" if magnitude >= 1 else "within_tolerance"
                body.update(deviation_absolute=format(difference,"f"), deviation_percent=None if percentage is None else format(percentage,"f"),
                            band=band, redraft_required=band == "overturn_candidate", actual_basis=picked["basis"])
            output.append({**body, "id": "model-cell-reconciliation:" + content_hash(body)[:32], "content_hash": content_hash(body)})
    return output


class ModelCellReconciliationAuthority:
    _authorized = authorized_flag()
    def __init__(self, store: Any):
        self.store, self.connection = store, store.connection
        self._authorization_flag = authorization_flag(self.connection,"dalton_model_cell_reconciliation_authorized")
        self.connection.executescript(_SCHEMA)

    def refresh(self, company_ref: str) -> list[dict[str, Any]]:
        if not _exists(self.connection,"forecast_model_versions"): return []
        actuals, events = collect_actuals(self.connection,company_ref)
        records = []
        for row in self.connection.execute("SELECT * FROM forecast_model_versions WHERE company_ref=? ORDER BY created_at", (company_ref,)):
            records.extend(compare_cells(_verified(row),actuals,events))
        self._authorized = True
        try:
            with self.store._transaction() as cur:
                for record in records:
                    cur.execute("INSERT OR IGNORE INTO forecast_model_cell_reconciliations VALUES(?,?,?,?,?,?,?,?,?)", (record["id"], company_ref,
                                record["model_version_ref"], record["cell_ref"], record["period"]["end"], record["status"], canonical_json(record), record["content_hash"], datetime.now(timezone.utc).isoformat()))
        finally: self._authorized = False
        from .research_followup import ResearchFollowupAuthority
        ResearchFollowupAuthority(self.store).capture_reconciliations(records)
        return records


def read_existing(connection: Any, company_ref: str) -> list[dict[str, Any]]:
    if not _exists(connection,"forecast_model_cell_reconciliations"): return []
    out = []
    for row in connection.execute("SELECT record_json,content_hash FROM forecast_model_cell_reconciliations WHERE company_ref=? ORDER BY created_at,reconciliation_id", (company_ref,)):
        record=json.loads(row["record_json"]);body={key:value for key,value in record.items() if key not in {"id","content_hash"}}
        if record["content_hash"] != row["content_hash"] or content_hash(body) != row["content_hash"] or record["id"] != "model-cell-reconciliation:"+content_hash(body)[:32]:
            raise ValueError("model cell reconciliation hash drifted")
        out.append(record)
    return out


def brief_entries(connection: Any, company_refs: Sequence[str], period_start: Any, period_end: Any) -> list[dict[str, Any]] | None:
    """Freeze exact model cells inside the declared issue window and scope."""
    if not _exists(connection, "forecast_model_cell_reconciliations"): return None
    output = []
    for company in company_refs:
        records = {record["id"]: record for record in read_existing(connection, company)}
        for row in connection.execute("SELECT reconciliation_id,created_at FROM forecast_model_cell_reconciliations WHERE company_ref=?", (company,)):
            created = datetime.fromisoformat(row["created_at"])
            if period_start < created <= period_end:
                record = records[row["reconciliation_id"]]
                output.append({"ref": record["id"], "hash": record["content_hash"], "company_ref": company,
                               "created_at": row["created_at"], "model_version_ref": record["model_version_ref"], "cell_ref": record["cell_ref"],
                               "period": record["period"], "status": record["status"], "reason": record["reason"],
                               "deviation_percent": record.get("deviation_percent"), "band": record.get("band"),
                               "redraft_required": record.get("redraft_required", False), "actual_basis": (record.get("actual") or {}).get("basis")})
    return sorted(output, key=lambda row: (row["created_at"], row["ref"]))


def replay_brief_entries(connection: Any, entries: Sequence[Mapping[str, Any]]) -> None:
    for entry in entries:
        records = {record["id"]: record for record in read_existing(connection, entry["company_ref"])}
        record = records.get(entry["ref"])
        row = connection.execute("SELECT created_at FROM forecast_model_cell_reconciliations WHERE reconciliation_id=?", (entry["ref"],)).fetchone()
        if not record or record["content_hash"] != entry["hash"] or not row or row["created_at"] != entry["created_at"]:
            raise ValueError("weekly model cell reconciliation binding differs")
        for key in ("model_version_ref", "cell_ref", "period", "status", "reason"):
            if record[key] != entry[key]: raise ValueError("weekly model cell reconciliation summary differs")
        for key in ("deviation_percent", "band", "redraft_required"):
            if record.get(key, False if key == "redraft_required" else None) != entry[key]:
                raise ValueError("weekly model cell reconciliation arithmetic differs")
        if (record.get("actual") or {}).get("basis") != entry["actual_basis"]: raise ValueError("weekly actual basis differs")
