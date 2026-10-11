"""The SEC filings this Core already fetched, as text the disclosure parsers can read.

**Why this exists.** The deterministic disclosure parsers -- the Item 2 / Item 5
issuer-purchases table (:mod:`buyback_disclosure`) and the Item 5 / Item 9B
trading-arrangement section (:mod:`insider_trading_plan`) -- were written
against ``document_index_documents``, the FTS projection.  That projection is
a separate SQLite file (``document-index.sqlite``) and the tracking lane reads
``core.sqlite``; on both live Cores the table was never there, the reader
caught "no such table" and returned nothing, and every run reported a clean
scan of zero filings.  Meanwhile the SEC discovery lane had fetched each
covered company's 10-K (legacy: five; ws-7d: four 10-Ks and seven 8-Ks) and
spooled the exact bytes.

**What this reads, and nothing else.**  Every row is a document the mission's
own SEC discovery lane recorded as ``acquired``.  For each one:

* the completed fetch manifest, through the read-only manifest reader the
  runtime build already uses (it cross-checks ticket, summary and manifest);
* the body bytes, from the content-addressed spool, re-hashed against the
  manifest's ``body_sha256`` -- a mismatch is a refusal, not a skip;
* the filing's form, filing date and filer name, from the governed
  ``list_filings`` submissions artifact the discovery itself was answered
  from (the same bytes :mod:`sec_earnings_release` reads), with the statement
  lane's filing ledger as the fallback when that artifact has been pruned;
* the URL is required to sit under the accession's own EDGAR directory, so a
  fetch can never be read as a different filing.

No network, no model, no write.  A filing that cannot be fully attributed is
reported with its reason and not returned.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .raw_spool import RawSpoolError, RawSpoolReader

SOURCE_REF = "source:sec-edgar"
DOCUMENT_REF_PREFIX = "sec:filing:"
SPOOL_ROOTS = ("transcript-spool", "connector-spool", "raw-spool")
#: The forms the disclosure parsers read.  Anything else the discovery lane
#: fetched (a proxy statement, an S-8) is not returned.
READABLE_FORMS: tuple[str, ...] = ("10-Q", "10-K", "10-Q/A", "10-K/A", "8-K")
#: A Microsoft 10-K is 8.6 MB of inline XBRL.  Bounded so a malformed spool
#: object cannot be decoded into memory without limit.
MAX_FILING_BYTES = 16_000_000
MAX_DOCUMENTS = 40


def _spool_bytes(state_dir: Path, digest: str) -> tuple[bytes | None, str | None]:
    for name in SPOOL_ROOTS:
        try:
            reader = RawSpoolReader(state_dir / name)
        except RawSpoolError:
            continue
        try:
            if not reader.object_exists(digest):
                continue
            raw = reader.read_object(digest)
        except RawSpoolError as exc:
            return None, f"{name} 中的原始文件无法读取：{exc}"
        if hashlib.sha256(raw).hexdigest() != digest:
            return None, f"{name} 中的原始文件哈希与记录的 {digest} 不符，拒绝读取"
        return raw, None
    return None, "本环境的原始文件库里已找不到这份申报的抓取内容"


def _submissions_row(state_dir: Path, connection: sqlite3.Connection,
                     discovery_ref: str | None, accession: str) -> dict[str, Any] | None:
    """Form, filing date and filer, from the submissions artifact the discovery read."""

    if not discovery_ref:
        return None
    try:
        row = connection.execute(
            "SELECT e.raw_response_hash AS hash FROM coverage_mission_source_discoveries d "
            "JOIN connector_source_envelopes e ON e.source_envelope_id=d.source_envelope_ref "
            "WHERE d.record_id=?", (discovery_ref,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or not row["hash"]:
        return None
    raw, _ = _spool_bytes(state_dir, row["hash"])
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
        if "filings" not in payload:
            from .sec_submissions_pages import submissions_payload,envelope_parameters,_record
            discovery=_record(connection,"coverage_mission_source_discoveries","record_id",discovery_ref)
            envelope=_record(connection,"connector_source_envelopes","source_envelope_id",discovery["source_envelope_ref"])
            payload=submissions_payload(payload,envelope_parameters(connection,envelope))
        recent = payload["filings"]["recent"]
        accessions = recent["accessionNumber"]
        index = accessions.index(accession)
        form = recent["form"][index]
        filed = recent["filingDate"][index]
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    if not isinstance(form, str) or not isinstance(filed, str) or len(filed) != 10:
        return None
    primary = None
    documents = recent.get("primaryDocument")
    if isinstance(documents, list) and index < len(documents):
        primary = documents[index]
    return {"form": form, "filing_date": filed,
            "issuer_name": payload.get("name") if isinstance(payload.get("name"), str) else None,
            "primary_document": primary if isinstance(primary, str) else None,
            "basis": "sec_submissions_artifact"}


def _statement_ledger_row(connection: sqlite3.Connection, accession: str) -> dict[str, Any] | None:
    try:
        row = connection.execute(
            "SELECT form, filed, entity_name FROM coverage_mission_statement_filings "
            "WHERE accession=? ORDER BY recorded_at DESC LIMIT 1", (accession,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    return {"form": row["form"], "filing_date": row["filed"],
            "issuer_name": row["entity_name"], "primary_document": None,
            "basis": "statement_filing_ledger"}


def _manifest_reader(state_dir: Path) -> Any | None:
    from .public_web_fetch_launcher import ReadOnlyPublicWebFetchManifestReader

    try:
        return ReadOnlyPublicWebFetchManifestReader(state_dir=state_dir)
    except Exception:  # noqa: BLE001 - a Core with no fetch tickets has no filings
        return None


def acquired_filing_documents(
    connection: sqlite3.Connection,
    *,
    company_ref: str,
    state_dir: str | Path | None,
    limit: int = MAX_DOCUMENTS,
    cache: dict[Any, Any] | None = None,
) -> dict[str, Any]:
    """Every attributable SEC filing the discovery lane fetched for this company.

    Returns ``{"documents": [...], "reports": [...]}``.  Each document has the
    shape :func:`buyback_disclosure.buyback_documents` returns, so both
    parsers read it unchanged; ``text`` is the filing's own markup, which is
    what the document index stores for an HTML filing as well.
    """

    if state_dir is None:
        return {"documents": [], "reports": []}
    state = Path(state_dir)
    from .mission_company_cik import index_company_refs

    refs = index_company_refs(connection, company_ref)
    try:
        rows = connection.execute(
            "SELECT document_ref, ticket_ref, discovery_ref, created_at "
            "FROM coverage_mission_discovered_documents "
            f"WHERE source_ref=? AND status='acquired' AND company_ref IN ({','.join('?' * len(refs))}) "
            "ORDER BY created_at DESC, record_id DESC",
            (SOURCE_REF, *refs),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return {"documents": [], "reports": []}
    seen: set[str] = set()
    candidates = []
    for row in rows:
        document_ref = row["document_ref"]
        if document_ref in seen or not str(document_ref).startswith(DOCUMENT_REF_PREFIX):
            continue
        seen.add(document_ref)
        candidates.append(row)
    if not candidates:
        return {"documents": [], "reports": []}
    # The buyback scan and the trading-plan scan both ask in one tracking pass;
    # a caller-owned ``cache`` (one per company per pass) reads and renders the
    # same filings once. Nothing is remembered beyond the caller's dict.
    memo_key = (str(state.resolve()), company_ref, int(limit),
                tuple((row["document_ref"], row["ticket_ref"]) for row in candidates))
    if cache is not None and memo_key in cache:
        return cache[memo_key]
    result = _read_candidates(connection, state, company_ref, candidates, limit)
    if cache is not None:
        cache[memo_key] = result
    return result


def _read_candidates(connection: sqlite3.Connection, state: Path, company_ref: str,
                     candidates: list[Any], limit: int) -> dict[str, Any]:
    reader = _manifest_reader(state)
    if reader is None:
        return {"documents": [], "reports": [{
            "status": "unavailable",
            "reason": "本环境没有抓取记录目录，无法核对申报原文"}]}
    documents: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    for row in candidates:
        document_ref = row["document_ref"]
        accession = document_ref[len(DOCUMENT_REF_PREFIX):]

        def refuse(reason: str) -> None:
            reports.append({"document_ref": document_ref, "accession": accession,
                            "status": "refused", "reason": reason})

        try:
            manifest = (reader.read_completed_manifest(row["ticket_ref"], document_ref)
                        if row["ticket_ref"] else reader.locate_completed_manifest(document_ref))
        except Exception as exc:  # noqa: BLE001 - one filing, not the scan
            refuse(f"没有完成的抓取清单：{exc}")
            continue
        url = str(manifest.get("canonical_url") or "")
        if accession.replace("-", "") not in url.split("?", 1)[0]:
            refuse("抓取的网址不在该申报编号的 EDGAR 目录下，不能当作这份申报")
            continue
        if int(manifest.get("body_bytes") or 0) > MAX_FILING_BYTES:
            refuse("申报原文超过读取上限")
            continue
        raw, problem = _spool_bytes(state, str(manifest.get("body_sha256") or ""))
        if raw is None:
            refuse(problem or "申报原文不可用")
            continue
        identity = (_submissions_row(state, connection, row["discovery_ref"], accession)
                    or _statement_ledger_row(connection, accession))
        if identity is None:
            refuse("申报索引和报表台账都没有记录该申报编号的表格类型与申报日期")
            continue
        primary = identity.get("primary_document")
        if primary and url.rsplit("/", 1)[-1] != primary:
            # An exhibit fetched under the accession is not the filing body.
            refuse("抓取的文件不是该申报的主文件（可能是附件）")
            continue
        form = str(identity["form"]).upper()
        if form not in READABLE_FORMS:
            reports.append({"document_ref": document_ref, "accession": accession,
                            "status": "skipped", "reason": f"{form} 表格不在本解析范围内"})
            continue
        issuer = identity.get("issuer_name") or company_ref
        rendered = None
        try:
            # The block rendering the extraction path uses; the issuer-purchases
            # table is read out of blocks, the inline XBRL out of the markup.
            from .public_web_extraction_source import render_public_web_text

            # ``legacy``: the disclosure parsers read a table one cell per
            # block, the shape they were written and tested against; the
            # inline-XBRL reader rendering (2026-10-09) joins a row.
            rendered = render_public_web_text(
                raw, raw_media_type=str(manifest.get("raw_media_type") or "text/html"),
                max_source_chars=MAX_FILING_BYTES,
                max_decompressed_bytes=MAX_FILING_BYTES, legacy=True)["text"]
        except Exception:  # noqa: BLE001 - the markup is still read for XBRL facts
            rendered = None
        documents.append({
            "artifact_version_ref": manifest.get("raw_artifact_version_ref") or document_ref,
            "artifact_hash": manifest["body_sha256"],
            "title": f"{issuer} {form}",
            "issuer_name": issuer,
            "form": form,
            "document_date": identity["filing_date"],
            "accession": accession,
            "text": raw.decode("utf-8", errors="replace"),
            "rendered_text": rendered,
            "document_ref": document_ref,
            "identity_basis": identity["basis"],
        })
        reports.append({"document_ref": document_ref, "accession": accession,
                        "status": "read", "form": form, "identity_basis": identity["basis"]})
        if len(documents) >= limit:
            break
    documents.sort(key=lambda item: (item["document_date"] or "", item["accession"]),
                   reverse=True)
    return {"documents": documents, "reports": reports}


__all__ = ["MAX_FILING_BYTES", "READABLE_FORMS", "SOURCE_REF", "acquired_filing_documents"]
