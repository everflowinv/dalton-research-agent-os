"""Bind two independently acquired SEC authorities across one project lineage.

The original document and statement parse keep their own immutable identities.
This is a target capability proof, not a numeric conclusion or new source grant.
The normal registry/admission path still re-reads the original bytes.
"""
from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit
import re
from typing import Any

from .store import content_hash

SCHEMA = "financial-note-source-lineage-0.1"
TARGET_SCHEMA = "financial-note-target-0.3"
FIELDS = frozenset({"schema_version", "company_ref", "document_ref", "registration_ref",
    "registration_hash", "acquired_authority_ref", "acquired_authority_hash",
    "acquired_mission_ref", "acquired_mission_hash", "statement_dispatch_ref",
    "statement_mission_ref", "statement_mission_hash", "statement_ingest_ref",
    "statement_filing_hash", "issuer_cik", "accession", "content_hash"})


def normalize_binding(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != FIELDS or value["schema_version"] != SCHEMA:
        raise ValueError("financial note source lineage has an invalid shape")
    body = {k: v for k, v in value.items() if k != "content_hash"}
    if (any(not isinstance(v, str) or not v for v in body.values())
            or any(re.fullmatch(r"[0-9a-f]{64}", body[k]) is None
                   for k in body if k.endswith("_hash"))
            or value["content_hash"] != content_hash(body)
            or re.fullmatch(r"[0-9]{10}", body["issuer_cik"]) is None
            or re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", body["accession"]) is None
            or body["document_ref"] != "sec:filing:" + body["accession"]):
        raise ValueError("financial note source lineage identity drifted")
    return dict(value)


def derive_binding(*, connection: Any, mission: Mapping[str, Any], company_ref: str,
                   registration: Mapping[str, Any], filing: Mapping[str, Any]) -> dict[str, Any]:
    from .coverage_mission import CoverageMissionAuthority
    from .document_mission_lineage import document_mission_versions
    from .document_research import validate_registration, CoreAcquiredDocumentSourceAdapter
    from .company_model_annual_projection import verify_statement_filing

    reg = validate_registration(registration)
    reader = object.__new__(CoverageMissionAuthority)
    reader.connection = connection
    versions = document_mission_versions(connection, mission)
    if (company_ref not in {r["company_ref"] for r in mission.get("universe", ())}
            or not any(s.get("source_ref") == "source:sec-edgar" and s.get("status") == "connected"
                       for s in mission.get("source_plan", ()))):
        raise ValueError("financial note current source or company is not authorized")
    authority = reg["source_authority"]
    row = connection.execute("SELECT * FROM coverage_mission_discovered_documents WHERE record_id=?",
                             (authority["ref"],)).fetchone()
    if row is None:
        raise ValueError("financial note acquired authority is unavailable")
    row = dict(row)
    acquired = CoreAcquiredDocumentSourceAdapter._authority(row)
    if (reg["source_ref"] != "source:sec-edgar" or row["source_ref"] != reg["source_ref"]
            or authority["kind"] != "coverage-mission-acquired-document"
            or authority["ref"] != acquired["record_id"] or authority["hash"] != acquired["content_hash"]
            or authority["mission_version_ref"] != row["mission_version_ref"]
            or authority["company_ref"] != company_ref or row["company_ref"] != company_ref
            or row["status"] != "acquired" or row["document_ref"] != reg["document_ref"]
            or not row["ticket_ref"] or row["ticket_ref"] != reg["acquisition_ticket_ref"]):
        raise ValueError("financial note acquired authority differs from its registration")
    dispatch = connection.execute("SELECT * FROM coverage_mission_statement_dispatches WHERE dispatch_id=?",
                                  (filing["dispatch_id"],)).fetchone()
    if dispatch is None:
        raise ValueError("financial note statement dispatch is unavailable")
    dispatch = dict(dispatch)
    for ref in (row["mission_version_ref"], dispatch["mission_version_ref"]):
        if ref not in versions:
            raise ValueError("financial note sources cross project lineage")
    original = reader._mission_record(row["mission_version_ref"])
    parsed = reader._mission_record(dispatch["mission_version_ref"])
    if (dispatch["mission_version_hash"] != parsed["content_hash"]
            or dispatch["company_ref"] != company_ref or filing["company_ref"] != company_ref
            or dispatch["status"] != "succeeded" or dispatch["form"] != filing["form"]
            or reg["document_ref"] != "sec:filing:" + filing["accession"]):
        raise ValueError("financial note statement source identity differs")
    # A filing agent's accession prefix is not the issuer CIK. Bind the actual
    # primary/attachment EDGAR issuer directory, not the accession's first ten digits.
    url = urlsplit(reg["origin_ref"] or "")
    match = re.fullmatch(r"/Archives/edgar/data/([0-9]+)/([0-9]{18})/[^/]+", url.path)
    cik = str(filing["cik"]).zfill(10)
    if (url.scheme != "https" or url.hostname not in {"www.sec.gov", "sec.gov"}
            or url.port not in {None, 443} or url.query or url.fragment or url.username or url.password or match is None
            or match[1].zfill(10) != cik or match[2] != filing["accession"].replace("-", "")):
        raise ValueError("financial note original issuer/accession URL differs from statement filing")
    if not json_source_refs(filing):
        raise ValueError("financial note statement source records are empty")
    verify_statement_filing(connection, filing)
    body = {"schema_version": SCHEMA, "company_ref": company_ref,
        "document_ref": reg["document_ref"], "registration_ref": reg["id"],
        "registration_hash": reg["content_hash"], "acquired_authority_ref": acquired["record_id"],
        "acquired_authority_hash": acquired["content_hash"], "acquired_mission_ref": original["id"],
        "acquired_mission_hash": original["content_hash"], "statement_dispatch_ref": dispatch["dispatch_id"],
        "statement_mission_ref": parsed["id"], "statement_mission_hash": parsed["content_hash"],
        "statement_ingest_ref": filing["ingest_id"], "statement_filing_hash": filing["content_hash"],
        "issuer_cik": cik, "accession": filing["accession"]}
    return normalize_binding({**body, "content_hash": content_hash(body)})


def json_source_refs(filing: Mapping[str, Any]) -> list[str]:
    import json
    refs = filing.get("source_record_refs")
    if refs is None:
        refs = json.loads(filing["source_record_refs_json"])
    if not isinstance(refs, list) or any(not isinstance(r, str) or not r for r in refs):
        raise ValueError("financial note statement source records are invalid")
    return refs
