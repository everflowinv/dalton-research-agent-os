"""Source-bound, prospective outcome targets. No inferred investment direction."""
from __future__ import annotations
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping
from .store import canonical_json, content_hash

RULE_REF = "judgement-outcome-registration:1"
FIELDS = frozenset({"judgement_ref", "thesis_version_ref", "thesis_hash", "direction", "metric_ref",
                    "benchmark_version_ref", "benchmark_hash", "period_start", "period_end",
                    "target_release_date", "information_cutoff", "threshold_percent", "basket_members", "source_refs"})

def _exists(connection: Any, table: str) -> bool:
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None

def _verified(row: Any) -> dict[str, Any]:
    wire = json.loads(row["record_json"])
    if wire.get("content_hash") != row["content_hash"] or content_hash({k:v for k,v in wire.items() if k != "content_hash"}) != row["content_hash"]:
        raise ValueError("outcome source hash drifted")
    return wire

def read_registration(connection: Any, judgement_ref: str) -> dict[str, Any] | None:
    if not _exists(connection, "outcome_registrations"): return None
    row = connection.execute("SELECT * FROM outcome_registrations WHERE judgement_ref=?", (judgement_ref,)).fetchone()
    return _verified(row) if row else None

def actual_publications(connection: Any, company_ref: str, period_end: str) -> list[dict[str, Any]]:
    """Dates use verified metadata; metadata alone never certifies numeric actuals."""
    events = []
    if _exists(connection, "coverage_mission_statement_filings"):
        from .company_model_annual_projection import verify_statement_filing
        for row in connection.execute("SELECT * FROM coverage_mission_statement_filings WHERE company_ref=? AND report_date=?", (company_ref, period_end)):
            verify_statement_filing(connection, dict(row), allow_legacy_metadata=True)
            events.append({"ref": row["ingest_id"], "hash": row["content_hash"], "published_on": row["filed"]})
    if _exists(connection, "preliminary_actuals"):
        for row in connection.execute("SELECT * FROM preliminary_actuals WHERE company_ref=? AND period_end=?", (company_ref, period_end)):
            record = _verified(row)
            read = _verified(connection.execute("SELECT * FROM earnings_release_reads WHERE read_id=?", (record["read_ref"],)).fetchone())
            if read["status"] != "parsed" or read["body_sha256"] != record["body_sha256"]:
                raise ValueError("outcome actual publication binding differs")
            event = {"ref": read["id"], "hash": read["content_hash"], "published_on": record["report_date"]}
            if event not in events: events.append(event)
    return events

def formal_thesis(connection: Any, company_ref: str, ref: str, digest: str, *, current: bool = False) -> dict[str, Any]:
    from .thesis_question_model import thesis_payload
    row = connection.execute("SELECT * FROM thesis_versions WHERE version_id=?", (ref,)).fetchone()
    if not row: raise ValueError("formal thesis version is missing")
    wire = json.loads(row["content_json"])
    if row["content_hash"] != digest or content_hash(thesis_payload(wire)) != digest:
        raise ValueError("formal thesis version hash differs")
    if wire.get("view_status", "active") != "active": raise ValueError("formal thesis is not active")
    # A loose industry-level view cannot silently become this company's view.
    subject = connection.execute("SELECT company_ref FROM thesis_admission_candidates WHERE thesis_ref=?", (wire["thesis_ref"],)).fetchone()
    if not subject or subject[0] != company_ref:
        raise ValueError("formal thesis lacks exact company scope")
    if current:
        pointer = connection.execute("SELECT version_id FROM current_pointers WHERE thesis_id=?", (wire["thesis_ref"],)).fetchone()
        if not pointer or pointer[0] != ref: raise ValueError("registration must bind the current formal thesis")
    return {**wire, "authority_created_at": row["created_at"]}

class OutcomeRegistrationAuthority:
    def __init__(self, store: Any, *, clock=None):
        self.store, self.connection = store, store.connection
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat(timespec="microseconds"))
        self.connection.executescript(Path(__file__).with_name("judgement_outcome_schema.sql").read_text())

    def record(self, draft: Mapping[str, Any], *, actor_ref: str, apply: bool = True) -> dict[str, Any]:
        if set(draft) != FIELDS: raise ValueError("outcome target requires the closed registration shape; timestamps are authority-owned")
        if not str(actor_ref).startswith("human:"): raise ValueError("outcome targets require an explicit owner registration")
        if draft["direction"] not in {"up", "down", "flat"}: raise ValueError("outcome direction must be explicitly up/down/flat")
        threshold = Decimal(str(draft["threshold_percent"]))
        if not threshold.is_finite() or threshold < 0: raise ValueError("outcome threshold must be a finite nonnegative number")
        for key in ("period_start", "period_end", "target_release_date", "information_cutoff"):
            date.fromisoformat(draft[key])
        registered = self.clock(); today = registered[:10]
        if draft["period_start"] > draft["period_end"] or draft["target_release_date"] < draft["period_end"]:
            raise ValueError("outcome period/release ordering differs")
        if draft["information_cutoff"] > today or draft["target_release_date"] <= today:
            raise ValueError("outcome registration must precede the target result; historical timestamps cannot be supplied")
        if not draft["source_refs"] or any(not isinstance(ref, str) or not ref.strip() for ref in draft["source_refs"]):
            raise ValueError("outcome registration requires explicit source references")
        from .thesis_question_model import require_evidence
        require_evidence(self.store, {"claim_refs": [ref for ref in draft["source_refs"] if not ref.startswith("doc-passage:")],
                                      "passage_refs": [ref for ref in draft["source_refs"] if ref.startswith("doc-passage:")]})
        row = self.connection.execute("SELECT * FROM event_judgements WHERE judgement_id=?", (draft["judgement_ref"],)).fetchone()
        if not row: raise ValueError("outcome judgement is missing")
        judgement = _verified(row)
        if str(row["created_at"])[:10] > today: raise ValueError("outcome judgement is future-dated")
        thesis = formal_thesis(self.connection, row["company_ref"], draft["thesis_version_ref"], draft["thesis_hash"], current=True)
        if thesis["authority_created_at"][:10] > str(row["created_at"])[:10] or draft["information_cutoff"] < str(row["created_at"])[:10]:
            raise ValueError("outcome view must be held at judgement time and within the declared cutoff")
        events = actual_publications(self.connection, row["company_ref"], draft["period_end"])
        if any(event["published_on"][:10] <= today for event in events): raise ValueError("outcome target was already published before registration")
        if draft["metric_ref"] == "price_excess_return":
            if draft["period_start"] < today or draft["period_end"] <= today or len(set(draft["basket_members"])) < 2 or row["company_ref"] in draft["basket_members"]:
                raise ValueError("price outcome needs a prospective window and an explicit comparison basket")
            expected = content_hash({"basket_members": sorted(set(draft["basket_members"]))})
            if draft["benchmark_hash"] != expected or draft["benchmark_version_ref"] != "outcome-basket:" + expected[:32]:
                raise ValueError("price basket version/hash differs from declared members")
        else:
            benchmark = self.connection.execute("SELECT * FROM forecast_model_versions WHERE version_id=?", (draft["benchmark_version_ref"],)).fetchone()
            if not benchmark: raise ValueError("outcome forecast benchmark is missing")
            model = _verified(benchmark)
            if model["content_hash"] != draft["benchmark_hash"] or model["company_ref"] != row["company_ref"] or model["created_at"][:10] > draft["information_cutoff"]:
                raise ValueError("outcome forecast benchmark hash/scope/cutoff differs")
            cells = [cell for result in model["results"] if result["ref"] == draft["metric_ref"] for cell in result["cells"]
                     if cell["period"]["start"] == draft["period_start"] and cell["period"]["end"] == draft["period_end"] and cell["kind"] == "estimate"]
            if len(cells) != 1: raise ValueError("outcome target must name one exact forecast cell")
            if draft["basket_members"]: raise ValueError("financial outcome does not take a price basket")
        body = {**dict(draft), "rule_ref": RULE_REF, "company_ref": row["company_ref"], "judgement_hash": judgement["content_hash"],
                "registered_at": registered, "actor_ref": actor_ref}
        body["id"] = "outcome-registration:" + content_hash(body)[:32]
        body["content_hash"] = content_hash(body)
        if not apply: return {**body, "status": "validated_dry_run"}
        with self.store._transaction() as cur:
            if cur.execute("SELECT 1 FROM outcome_registrations WHERE judgement_ref=?", (draft["judgement_ref"],)).fetchone():
                raise ValueError("outcome target is already frozen for this judgement")
            cur.execute("INSERT INTO outcome_registrations VALUES(?,?,?,?,?,?)", (body["id"], draft["judgement_ref"], row["company_ref"], canonical_json(body), body["content_hash"], registered))
        return read_registration(self.connection, draft["judgement_ref"])
