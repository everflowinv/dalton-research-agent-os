"""Read-only acceptance audit for the first live research products."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

PRODUCTS = ("company_dossier", "debate_map", "event_judgement")
GRANTS = {"company_dossier": "dossier", "debate_map": "debate_map",
          # A judgement row itself is the lane ledger; ``deliverable`` is the
          # minimum mission scope the producer requires before spending.
          "event_judgement": "deliverable"}
CONFIGS = {
    "company_dossier": ("dossier-model-config.json", "company-dossier-verifier-model-config.json",
                        "p12a-dossier-policy-v1.json"),
    "debate_map": ("initial-screen-model-config.json",),
    "event_judgement": ("event-judgement-model-config.json", "event-verifier-model-config.json"),
}


@contextmanager
def open_readonly(path: Path):
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")  # One consistent snapshot for all audit reads.
        yield connection
    finally:
        connection.close()


def _table(c: sqlite3.Connection, name: str) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _record(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    try:
        value = json.loads(row["record_json"])
    except (TypeError, json.JSONDecodeError, IndexError):
        return None
    return value if isinstance(value, dict) else None


def _mission(c: sqlite3.Connection, mission_ref: str | None) -> dict[str, Any]:
    where, args = ("WHERE p.mission_ref=?", (mission_ref,)) if mission_ref else ("", ())
    rows = c.execute(
        "SELECT p.mission_ref,p.mission_version_id,v.record_json,v.content_hash "
        "FROM coverage_mission_pointer p JOIN coverage_mission_versions v "
        "ON v.mission_version_id=p.mission_version_id " + where + " ORDER BY p.mission_ref", args).fetchall()
    if len(rows) != 1:
        raise ValueError(f"expected exactly one current mission, found {len(rows)}")
    mission = _record(rows[0])
    if mission is None:
        raise ValueError("current mission record_json is invalid")
    mission["_stored_hash"] = rows[0]["content_hash"]
    return mission


def _claims(c: sqlite3.Connection, company_ref: str) -> list[str]:
    if not _table(c, "claim_versions"):
        return []
    # Match query_company_research/subject_claim_refs: latest version per
    # claim_ref, canonical index projection, claim_ref order, and 1,000 limit.
    # Reuse the pure index reader, which validates immutable entry hashes.
    from .company_research_view import annotate_with_index
    rows = c.execute(
        "SELECT v.claim_version_id,v.claim_json FROM claim_versions v "
        "WHERE NOT EXISTS (SELECT 1 FROM claim_versions x WHERE x.claim_ref=v.claim_ref "
        "AND (x.version_number>v.version_number OR "
        "(x.version_number=v.version_number AND x.claim_version_id>v.claim_version_id))) "
        "ORDER BY v.claim_ref").fetchall()
    found = []
    for row in rows:
        body = json.loads(row["claim_json"])
        if body.get("subject_ref") == company_ref:
            found.append({"claim_version_ref": row["claim_version_id"]})
    return [row["claim_version_ref"] for row in annotate_with_index(c, found)[:1000]]


def _digest(refs: list[str]) -> str:
    wire = json.dumps({"claim_version_refs": sorted(set(refs))}, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(wire.encode()).hexdigest()


def _latest(c: sqlite3.Connection, table: str, company_ref: str) -> tuple[dict[str, Any] | None, str | None]:
    if not _table(c, table):
        return None, None
    if table == "company_dossier_versions":
        row = c.execute("SELECT record_json,content_hash FROM company_dossier_versions WHERE company_ref=? ORDER BY version_number DESC LIMIT 1", (company_ref,)).fetchone()
    elif table == "debate_map_versions":
        row = c.execute("SELECT record_json,content_hash FROM debate_map_versions WHERE subject_ref=? AND subject_kind='company' ORDER BY version_number DESC LIMIT 1", (company_ref,)).fetchone()
    else:
        row = c.execute("SELECT record_json,content_hash FROM event_judgements WHERE company_ref=? ORDER BY created_at DESC,judgement_id DESC LIMIT 1", (company_ref,)).fetchone()
    return _record(row), None if row is None else row["content_hash"]


def _blocked_reasons(product: str, mission: dict[str, Any], state: Path,
                     claim_refs: list[str], unjudged: int, connection: sqlite3.Connection,
                     company_ref: str) -> list[str]:
    reasons = []
    may_write = set((mission.get("autonomy") or {}).get("may_write") or ())
    if GRANTS[product] not in may_write:
        reasons.append(f"mission_missing_grant:{GRANTS[product]}")
    missing, invalid, route_refs = [], [], []
    for name in CONFIGS[product]:
        path = state / name
        if not path.is_file():
            missing.append(name)
            continue
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            invalid.append(name)
            continue
        if name == "p12a-dossier-policy-v1.json":
            from .company_dossier import validate_policy
            try:
                validate_policy(body)
            except ValueError:
                invalid.append(name)
        if name.endswith("model-config.json"):
            ref = body.get("routing_policy_ref") if isinstance(body, dict) else None
            if not isinstance(ref, str) or not ref:
                invalid.append(name)
            else:
                route_refs.append(ref)
    if missing:
        reasons.append("missing_config:" + ",".join(missing))
    if invalid:
        reasons.append("invalid_config:" + ",".join(invalid))
    router_path = state / "model-router.sqlite"
    if route_refs and router_path.is_file():
        try:
            with open_readonly(router_path) as router:
                absent = [ref for ref in route_refs if router.execute(
                    "SELECT 1 FROM model_routing_policy_versions WHERE policy_version_ref=?",
                    (ref,)).fetchone() is None]
            if absent:
                reasons.append("missing_model_route:" + ",".join(absent))
        except sqlite3.Error as exc:
            reasons.append(f"model_router_unreadable:{type(exc).__name__}")
    elif route_refs:
        reasons.append("model_router_unreadable:missing_database")
    if product == "company_dossier":
        if not _table(connection, "claim_index_entry_versions"):
            reasons.append("no_eligible_input:no_claim_index")
        statuses = []
        if _table(connection, "coverage_mission_stage_records"):
            for table, status in (("coverage_mission_stage_records", "r.status"),
                                  ("coverage_mission_stage_reopens", "'reopened'")):
                if _table(connection, table):
                    statuses.extend(connection.execute(
                        f"SELECT r.created_at,r.record_id,{status} AS status FROM {table} r "
                        "JOIN coverage_mission_versions v ON v.mission_version_id=r.mission_version_ref "
                        "WHERE v.mission_ref=? AND r.company_ref=? AND r.stage_ref='initial_screen'",
                        (mission["mission_ref"], company_ref)).fetchall())
        from .coverage_mission import fold_stage_status
        if fold_stage_status([row["status"] for row in sorted(statuses, key=lambda r: (r["created_at"], r["record_id"]))]) != "gate_passed":
            reasons.append("no_eligible_input:initial_screen_not_passed")
    if product in {"company_dossier", "debate_map"} and not claim_refs:
        reasons.append("no_eligible_input:no_current_company_claims")
    if product == "event_judgement" and not unjudged:
        reasons.append("no_eligible_input:no_unjudged_company_events")
    return reasons or ["eligible_input_waiting_or_lane_refusal; inspect lane failure ledger"]


def audit(*, core_db: Path, state_dir: Path, mission_ref: str | None = None) -> dict[str, Any]:
    with open_readonly(core_db) as c:
        mission = _mission(c, mission_ref)
        companies = list(mission.get("universe") or ())
        out = {"schema_version": "0.1", "mode": "read_only", "core_db": str(core_db),
               "mission": {"ref": mission.get("mission_ref"), "version_ref": mission.get("id"),
                           "hash": mission.get("content_hash")}, "companies": []}
        for member in companies:
            company_ref = member["company_ref"]
            claims = _claims(c, company_ref)
            unjudged = 0
            if _table(c, "research_events") and _table(c, "event_judgements"):
                unjudged = c.execute(
                    "SELECT COUNT(*) FROM research_events e LEFT JOIN event_judgements j ON j.event_ref=e.event_id WHERE e.company_ref=? AND j.event_ref IS NULL",
                    (company_ref,)).fetchone()[0]
            products = {}
            for product, table in (("company_dossier", "company_dossier_versions"),
                                   ("debate_map", "debate_map_versions"),
                                   ("event_judgement", "event_judgements")):
                record, stored_hash = _latest(c, table, company_ref)
                item: dict[str, Any] = {"status": "missing"}
                if record is None:
                    item["blockers"] = _blocked_reasons(
                        product, mission, state_dir, claims, unjudged, c, company_ref)
                    item["reason"] = item["blockers"][0]
                else:
                    item.update({"status": "present", "ref": record.get("id"),
                                 "hash": stored_hash, "created_at": record.get("created_at")})
                    if product == "company_dossier":
                        bound = ((record.get("bindings") or {}).get("mission_version_ref"))
                        item["mission_binding"] = {"ref": bound, "fresh": bound == mission.get("id")}
                        cited = {r.get("ref") for r in record.get("evidence_refs") or [] if isinstance(r, dict)}
                        item["input_binding"] = {
                            "method": "evidence_scope_only",
                            "fresh": None,
                            "bound_refs_valid": cited.issubset(set(claims)),
                            "reason": ("CompanyDossierVersion persists cited evidence but no exact "
                                       "producer input fingerprint; current-input freshness is not provable")}
                    elif product == "debate_map":
                        current = _digest(claims)
                        item["mission_binding"] = {"fresh": None, "reason": "DebateMapVersion has no mission binding field"}
                        item["input_binding"] = {"method": "evidence_fingerprint",
                                                 "stored": record.get("evidence_fingerprint"),
                                                 "current": current,
                                                 "fresh": record.get("evidence_fingerprint") == current}
                    else:
                        event = c.execute("SELECT content_hash FROM research_events WHERE event_id=?", (record.get("event_ref"),)).fetchone() if _table(c, "research_events") else None
                        item["mission_binding"] = {"ref": record.get("mission_version_ref"),
                                                   "fresh": record.get("mission_version_ref") == mission.get("id")}
                        item["input_binding"] = {"event_ref": record.get("event_ref"),
                                                 "stored_hash": record.get("event_hash"),
                                                 "current_hash": None if event is None else event[0],
                                                 "fresh": event is not None and record.get("event_hash") == event[0]}
                products[product] = item
            out["companies"].append({"company_ref": company_ref, "ticker": member.get("ticker"),
                                     "eligible_claims": len(claims), "unjudged_events": unjudged,
                                     "products": products})
        return out


def markdown(report: dict[str, Any]) -> str:
    lines = [f"# Activation readiness — {report['mission']['version_ref']}", "",
             "| Company | Product | Status | Ref | Freshness / reason |", "|---|---|---|---|---|"]
    for company in report["companies"]:
        for name, item in company["products"].items():
            checks = [item.get("reason", "")]
            for key in ("mission_binding", "input_binding"):
                if key in item:
                    checks.append(f"{key}={item[key].get('fresh')}")
            lines.append(f"| {company.get('ticker') or company['company_ref']} | {name} | {item['status']} | {item.get('ref') or '-'} | {'; '.join(x for x in checks if x)} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--state-dir", type=Path, required=True)
    p.add_argument("--core-db", type=Path)
    p.add_argument("--mission-ref")
    p.add_argument("--json-output", type=Path)
    p.add_argument("--markdown-output", type=Path)
    args = p.parse_args(argv)
    core_db = args.core_db or args.state_dir / "core.sqlite"
    outputs = [path.resolve() for path in (args.json_output, args.markdown_output) if path]
    if len(outputs) != len(set(outputs)):
        p.error("audit output paths must be distinct")
    for output in outputs:
        if output.is_relative_to(args.state_dir.resolve()) or output == core_db.resolve():
            p.error("audit outputs must be outside the source state and Core database")
    report = audit(core_db=core_db,
                   state_dir=args.state_dir, mission_ref=args.mission_ref)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.json_output: args.json_output.write_text(encoded, encoding="utf-8")
    if args.markdown_output: args.markdown_output.write_text(markdown(report), encoding="utf-8")
    if not args.json_output: print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
