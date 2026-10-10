"""Source-bound catalyst outcomes and preview observation points, without calls."""
from __future__ import annotations

import json
from typing import Any, Mapping
from .store import canonical_json, content_hash, authorization_flag, authorized_flag

RULE_REF = "research-lifecycle:source-bound:1"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS earnings_preview_observations (
 observation_id TEXT PRIMARY KEY, company_ref TEXT NOT NULL,
 preview_version_ref TEXT NOT NULL UNIQUE, entry_ref TEXT NOT NULL,
 record_json TEXT NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS earnings_preview_observations_authorized_insert
BEFORE INSERT ON earnings_preview_observations WHEN dalton_preview_observation_authorized()=0
BEGIN SELECT RAISE(ABORT,'preview observations require verified publication'); END;
CREATE TRIGGER IF NOT EXISTS earnings_preview_observations_no_update BEFORE UPDATE ON earnings_preview_observations
BEGIN SELECT RAISE(ABORT,'preview observations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS earnings_preview_observations_no_delete BEFORE DELETE ON earnings_preview_observations
BEGIN SELECT RAISE(ABORT,'preview observations are immutable'); END;
"""


def _exists(connection: Any, table: str) -> bool:
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _verified(row: Any) -> dict[str, Any]:
    record = json.loads(row["record_json"])
    body = dict(record)
    digest = body.pop("content_hash", None)
    if digest != row["content_hash"] or digest != content_hash(body):
        raise ValueError("lifecycle source record hash drifted")
    return record


class PreviewObservationAuthority:
    _authorized = authorized_flag()

    def __init__(self, store: Any):
        self.store, self.connection = store, store.connection
        self._authorization_flag = authorization_flag(self.connection, "dalton_preview_observation_authorized")
        self.connection.executescript(_SCHEMA)

    def record(self, preview: Mapping[str, Any], context: Mapping[str, Any], draft: Mapping[str, Any]) -> dict[str, Any]:
        from .earnings_preview import OUTPUT_KEYS, validate_preview_output, preview_body, document_summary
        output = validate_preview_output({key: draft[key] for key in OUTPUT_KEYS}, context)
        occurrence = context["occurrence"]
        row = self.connection.execute("SELECT * FROM mission_deliverable_versions WHERE version_id=?", (preview["id"],)).fetchone()
        held = _verified(row)
        if held["subject_ref"] != occurrence["company_ref"] or held["content_hash"] != preview["content_hash"]:
            raise ValueError("preview observation source binding differs")
        if held["sections"][0]["body"] != preview_body(output, context) or held["summary"] != document_summary(context):
            raise ValueError("preview observation does not replay the published document")
        points = [{"text": item["question"], "why": item["why"], "refs": item["refs"]}
                  for item in output["what_to_watch"]]
        if any(not item["refs"] for item in points):
            raise ValueError("preview observation needs an exact cited source")
        body = {"rule_ref": RULE_REF, "company_ref": occurrence["company_ref"],
                "entry_ref": occurrence["entry_ref"], "expected_date": occurrence["expected_date"],
                "preview_version_ref": preview["id"], "preview_hash": preview["content_hash"],
                "context_hash": content_hash(context), "key_observations": points}
        record = {**body, "content_hash": content_hash(body)}
        self._authorized = True
        try:
            with self.store._transaction() as cur:
                cur.execute("INSERT OR IGNORE INTO earnings_preview_observations VALUES(?,?,?,?,?,?,?)",
                            ("preview-observations:" + record["content_hash"][:32], body["company_ref"], preview["id"],
                             body["entry_ref"], canonical_json(record), record["content_hash"], preview["created_at"]))
        finally:
            self._authorized = False
        return record


def observation_points(connection: Any, company_ref: str, entry: Mapping[str, Any], today: str) -> dict[str, Any]:
    if not _exists(connection, "earnings_preview_observations"):
        return {"key_observations": [], "observations_status": "missing", "observations_reason": "尚无来源绑定的前瞻观察条件"}
    from .product_consistency import binding_status_index
    current = binding_status_index(connection, company_ref, today=today)
    for row in connection.execute("SELECT * FROM earnings_preview_observations WHERE company_ref=? AND entry_ref=? "
                                  "ORDER BY created_at DESC,observation_id DESC", (company_ref, entry["entry_ref"])):
        record = _verified(row)
        if record["expected_date"] != entry["expected_date"]:
            continue
        preview_row = connection.execute("SELECT * FROM mission_deliverable_versions WHERE version_id=?", (record["preview_version_ref"],)).fetchone()
        if preview_row is None or _verified(preview_row)["content_hash"] != record["preview_hash"]:
            raise ValueError("preview observation source changed")
        status = (current.get(record["preview_version_ref"]) or {}).get("binding_status")
        if status != "current":
            continue
        return {"key_observations": [{**point, "label": point["text"], "preview_version_ref": record["preview_version_ref"]}
                                     for point in record["key_observations"]], "observations_status": "available"}
    return {"key_observations": [], "observations_status": "missing", "observations_reason": "前瞻观察条件缺失、已过期或绑定旧输入"}


def catalyst_status(connection: Any, company_ref: str, entry: Mapping[str, Any], today: str) -> dict[str, Any]:
    results = []
    if _exists(connection, "event_judgements"):
        for row in connection.execute("SELECT record_json,content_hash FROM event_judgements WHERE company_ref=? ORDER BY created_at", (company_ref,)):
            record = _verified(row)
            material = record.get("result_material") or {}
            if material.get("catalyst_entry_ref") == entry["entry_ref"]:
                results.append(record.get("id") or record.get("judgement_id"))
    if results:
        status = "occurred"
    elif entry["expected_date"] >= today:
        status = "scheduled"
    else:
        status = "missed" if entry.get("confidence") == "confirmed" else "unconfirmed"
    return {"lifecycle_rule_ref": RULE_REF, "status": status, "result_refs": [ref for ref in results if ref],
            "result_status": "linked" if results else "not_observed", "event_date": entry["expected_date"],
            "information_available_at": [source.get("observed_at") for source in entry.get("sources") or []],
            "may_trigger_judgement": entry.get("confidence") == "confirmed" and bool(entry.get("sources"))}
