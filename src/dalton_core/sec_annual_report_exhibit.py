"""A 10-K's Exhibit 13, when the 10-K incorporates its annual report by reference (2026-10-09).

Some issuers file the annual report to security holders as Exhibit 13 and
incorporate it into Parts I, II and IV of the 10-K.  The 10-K body then holds
no MD&A tables and no segment table: live, IBM's FY2025 10-K says "Portions
of IBM's Annual Report to Stockholders ... are incorporated by reference" and
links ``ibm-20251231_d2.htm`` from the exhibit index row "(13) Annual Report
to Security Holders".  Two directed reads asked that 10-K for the consulting
segment's revenue and profit and found nothing, because the answer is in a
document nobody had fetched.

This module decides, from the 10-K's own bytes, whether there is such an
exhibit and which file it is; the fetch child fetches it beside the 10-K
under the same governance (``public_web_fetch_cli.fetch_annual_report_exhibit``)
and records the outcome in the 10-K's ticket directory; readers then read
the 10-K and its Exhibit 13 as one registered document
(``document_research.PublicWebDocumentSourceAdapter``).  A 10-K fetched
before this existed is visited once by the exhibit re-detection the
discovery lane already runs for 8-Ks.

Read-only here: no network, no write.
"""

from __future__ import annotations

import json
import re
import sqlite3
from html import unescape
from pathlib import Path
from typing import Any, Mapping

DETECTOR_VERSION = "annual-report-exhibit:0.1"
#: Beside the 10-K's own ``manifest.json`` and ``summary.json``.
MANIFEST_FILE = "annual-report-exhibit-manifest.json"
RECORD_FILE = "annual-report-exhibit.json"
ANNUAL_REPORT_SPEC_REF = "annual-report-10k"
MAX_EXHIBIT_BYTES = 16_000_000

_TAG_RE = re.compile(r"<[^>]+>")
_HREF_RE = re.compile(r"<a\b[^>]*\bhref\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
                      re.IGNORECASE | re.DOTALL)
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.(?:htm|html|txt)")
_ANNUAL_WORDS = re.compile(
    r"annual\s+report\s+to\s+(?:stock|share|security\s+)holders", re.IGNORECASE)
_INCORPORATED = re.compile(r"incorporated\s+(?:herein\s+)?by\s+reference", re.IGNORECASE)
_EXHIBIT_13 = re.compile(r"^(?:\(?\s*13(?:\.\d{1,2})?\s*\)?|exhibit\s+13(?:\.\d{1,2})?)$",
                         re.IGNORECASE)


def _plain(fragment: str) -> str:
    return re.sub(r"\s+", " ", unescape(_TAG_RE.sub(" ", fragment))).replace("\xa0", " ").strip()


def _text(value: str | bytes) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, (bytes, bytearray)) else str(value)


def incorporates_annual_report(primary_html: str | bytes) -> bool:
    """Whether the 10-K says its annual report to holders is incorporated by reference."""

    text = _plain(_text(primary_html))
    for match in _ANNUAL_WORDS.finditer(text):
        window = text[max(0, match.start() - 400): match.end() + 400]
        if _INCORPORATED.search(window):
            return True
    return False


def annual_report_exhibit_name(primary_html: str | bytes, *, accession: str | None = None) -> str | None:
    """The file name of Exhibit 13 from the 10-K's own exhibit index, or None.

    The row whose first non-empty cell is the exhibit number 13 (``13``,
    ``(13)``, ``13.1``) and whose text names the annual report to holders; its
    one link, as a plain file name in the filing's own directory.  An absolute
    EDGAR link counts only inside the accession's own directory.  Anything
    ambiguous is None.
    """

    html = _text(primary_html)
    names: list[str] = []
    compact = None if accession is None else accession.replace("-", "")
    for row in re.finditer(r"<tr\b.*?</tr>", html, re.IGNORECASE | re.DOTALL):
        body = row.group(0)
        cells = [_plain(cell) for cell in re.findall(
            r"<t[dh]\b[^>]*>(.*?)</t[dh]>", body, re.IGNORECASE | re.DOTALL)]
        first = next((cell for cell in cells if cell), "")
        if not _EXHIBIT_13.match(first) or not _ANNUAL_WORDS.search(_plain(body)):
            continue
        for match in _HREF_RE.finditer(body):
            href = match.group(1).strip()
            if "?" in href or "#" in href:
                continue
            if "://" in href:
                if (compact is None or not href.startswith("https://www.sec.gov/Archives/edgar/data/")
                        or f"/{compact}/" not in href):
                    continue
            elif "/" in href:
                continue
            name = href.rsplit("/", 1)[-1]
            if _NAME_RE.fullmatch(name) and name not in names:
                names.append(name)
    return names[0] if len(names) == 1 else None


def detect(primary_html: str | bytes, *, accession: str | None = None) -> dict[str, Any]:
    """``{"incorporated": bool, "name": str | None}`` for one 10-K body."""

    incorporated = incorporates_annual_report(primary_html)
    name = annual_report_exhibit_name(primary_html, accession=accession) if incorporated else None
    return {"incorporated": incorporated, "name": name, "detector_version": DETECTOR_VERSION}


def _owner_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _ticket_dir(state_dir: Path, ticket_ref: str) -> Path | None:
    if not isinstance(ticket_ref, str) or not ticket_ref.startswith("public-web-fetch:"):
        return None
    return Path(state_dir) / "fetches" / ticket_ref.split(":", 1)[1]


def exhibit_outcome(state_dir: Path, ticket_ref: str) -> Mapping[str, Any] | None:
    """The recorded Exhibit 13 outcome for one 10-K fetch ticket, newest first."""

    directory = _ticket_dir(state_dir, ticket_ref)
    if directory is None:
        return None
    record = _owner_json(directory / RECORD_FILE)
    if isinstance(record, Mapping) and isinstance(record.get("exhibit"), Mapping):
        return record["exhibit"]
    summary = _owner_json(directory / "summary.json")
    if isinstance(summary, Mapping) and isinstance(summary.get("annual_report_exhibit"), Mapping):
        return summary["annual_report_exhibit"]
    return None


def exhibit_due(state_dir: Path, ticket_ref: str) -> bool:
    """A succeeded 10-K fetch with no Exhibit 13 outcome under this detector."""

    directory = _ticket_dir(state_dir, ticket_ref)
    if directory is None:
        return False
    summary = _owner_json(directory / "summary.json")
    if (not isinstance(summary, Mapping) or summary.get("status") != "succeeded"
            or not str(summary.get("url_ref") or "").startswith("sec:filing:")):
        return False
    outcome = exhibit_outcome(state_dir, ticket_ref)
    return not (isinstance(outcome, Mapping)
                and outcome.get("detector_version") == DETECTOR_VERSION)


def redetect_candidates(connection: sqlite3.Connection, state_dir: Path, *,
                        limit: int = 1) -> list[dict[str, Any]]:
    """Acquired 10-Ks whose Exhibit 13 has not been looked for yet."""

    try:
        rows = connection.execute(
            "SELECT DISTINCT d.company_ref,d.document_ref,d.ticket_ref "
            "FROM coverage_mission_discovered_documents d "
            "JOIN coverage_mission_source_discoveries s ON s.record_id=d.discovery_ref "
            "WHERE d.source_ref='source:sec-edgar' AND d.status='acquired' "
            "AND d.ticket_ref IS NOT NULL AND s.spec_ref=? ORDER BY d.document_ref",
            (ANNUAL_REPORT_SPEC_REF,)).fetchall()
    except sqlite3.Error:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        ticket = row[2]
        if ticket in seen or not exhibit_due(Path(state_dir), ticket):
            continue
        seen.add(ticket)
        out.append({"company_ref": row[0], "document_ref": row[1], "ticket_ref": ticket,
                    "accession": str(row[1]).removeprefix("sec:filing:"),
                    "kind": "annual_report_exhibit"})
        if len(out) >= limit:
            break
    return out


def exhibit_for_ticket(state_dir: Path, ticket_ref: str, document_ref: str) -> dict[str, Any]:
    """The fetched Exhibit 13 of one 10-K ticket, re-hashed, or why there is none."""

    directory = _ticket_dir(state_dir, ticket_ref)
    if directory is None:
        return {"status": "no_ticket"}
    summary = _owner_json(directory / "summary.json")
    if (not isinstance(summary, Mapping) or summary.get("status") != "succeeded"
            or summary.get("url_ref") != document_ref):
        return {"status": "no_ticket"}
    outcome = exhibit_outcome(state_dir, ticket_ref)
    if not isinstance(outcome, Mapping):
        return {"status": "not_attempted"}
    if outcome.get("status") != "succeeded":
        return {"status": str(outcome.get("status") or "failed"),
                "reason": outcome.get("failure_reason")}
    manifest = _owner_json(directory / MANIFEST_FILE)
    if not isinstance(manifest, Mapping):
        return {"status": "unavailable", "reason": "Exhibit 13 manifest is missing"}
    from .public_web_core_fetch import PublicWebCoreFetchError, validate_public_web_fetch_manifest

    try:
        manifest = validate_public_web_fetch_manifest(dict(manifest))
    except (PublicWebCoreFetchError, ValueError) as exc:
        return {"status": "unavailable", "reason": f"Exhibit 13 manifest is invalid: {exc}"}
    if (outcome.get("manifest_ref") != manifest["id"]
            or outcome.get("manifest_hash") != manifest["content_hash"]):
        return {"status": "unavailable", "reason": "Exhibit 13 outcome and manifest disagree"}
    accession = document_ref.removeprefix("sec:filing:")
    if f"/{accession.replace('-', '')}/" not in str(manifest["canonical_url"]):
        return {"status": "unavailable", "reason": "Exhibit 13 is not in this 10-K's directory"}
    if int(manifest.get("body_bytes") or 0) > MAX_EXHIBIT_BYTES:
        return {"status": "unavailable", "reason": "Exhibit 13 exceeds the byte bound"}
    from .acquired_sec_filings import _spool_bytes

    raw, reason = _spool_bytes(Path(state_dir), manifest["body_sha256"])
    if raw is None:
        return {"status": "unavailable", "reason": reason}
    return {"status": "fetched", "manifest": manifest, "bytes": raw,
            "exhibit_url": manifest["canonical_url"]}


__all__ = [
    "ANNUAL_REPORT_SPEC_REF", "DETECTOR_VERSION", "MANIFEST_FILE", "RECORD_FILE",
    "annual_report_exhibit_name", "detect", "exhibit_due", "exhibit_for_ticket",
    "exhibit_outcome", "incorporates_annual_report", "redetect_candidates",
]

class AnnualReportExhibitConflict(ValueError):
    pass


def verified_exhibit_source(core: Any, spool: Any, state_dir: Path, *,
                            ticket_ref: str, parent_document_ref: str,
                            company_ref: str, receipt_reader: Any,
                            max_source_chars: int = 6_000_000,
                            max_pdf_pages: int = 100,
                            max_decompressed_bytes: int = 32_000_000) -> dict[str, Any] | None:
    """One independently acquired Exhibit 13; never an appended 10-K body.

    Re-prove the parent and attachment fetch receipts, the 10-K's own exhibit
    link, and the issuer/accession directory before deriving the same SEC
    exhibit identity used by the corpus. A declared successful cache which is
    missing, truncated or inconsistent refuses instead of reading the parent.
    """
    from urllib.parse import urlsplit
    from .public_web_extraction_source import verified_public_web_source
    from .public_web_core_fetch import validate_public_web_fetch_manifest
    from .mission_company_cik import cik_in_ref
    from .store import content_hash

    def reject(message):
        raise AnnualReportExhibitConflict(message)

    class ExactReceipts:
        def __getattr__(self, name):
            reader=getattr(receipt_reader,name)
            def read(ref):
                record=reader(ref)
                if record is not None and (not isinstance(record,Mapping) or record.get('id')!=ref
                        or content_hash({k:v for k,v in record.items() if k!='content_hash'})!=record.get('content_hash')):
                    reject('annual report attachment Core receipt bytes/hash differ')
                return record
            return read
    exact_receipts=ExactReceipts()

    directory = _ticket_dir(Path(state_dir), ticket_ref)
    if directory is None or not re.fullmatch(r'sec:filing:\d{10}-\d{2}-\d{6}',parent_document_ref):
        reject('annual report attachment has no exact parent filing ticket')
    outcome = exhibit_outcome(Path(state_dir),ticket_ref)
    if outcome is None or outcome.get('status') != 'succeeded':
        return None
    summary=_owner_json(directory/'summary.json')
    cached=_owner_json(directory/MANIFEST_FILE)
    if not isinstance(summary,Mapping) or summary.get('status')!='succeeded' or summary.get('url_ref')!=parent_document_ref or not isinstance(cached,Mapping):
        reject('successful annual report attachment cannot be re-proved from its cached parent/manifest')
    child=validate_public_web_fetch_manifest(dict(cached))
    if (outcome.get('manifest_ref')!=child['id'] or outcome.get('manifest_hash')!=child['content_hash']
            or child['body_bytes']>MAX_EXHIBIT_BYTES):
        reject('successful annual report attachment manifest binding differs')
    parent = _owner_json(directory/'manifest.json')
    if not isinstance(parent,Mapping):
        reject('annual report attachment parent manifest differs')
    accession = parent_document_ref.removeprefix('sec:filing:')

    def sec_path(manifest):
        split=urlsplit(str(manifest.get('canonical_url') or ''))
        match=re.fullmatch(r'/Archives/edgar/data/(\d+)/(\d{18})/([A-Za-z0-9._-]+)',split.path)
        if (split.scheme != 'https' or split.hostname not in {'www.sec.gov','sec.gov'}
                or split.port not in {None,443} or split.query or split.fragment or match is None
                or match[2] != accession.replace('-','')):
            reject('annual report attachment is outside the exact SEC filing directory')
        return match

    parent_path=sec_path(parent);child_path=sec_path(child)
    for manifest,path in ((parent,parent_path),(child,child_path)):
        final=sec_path({'canonical_url':manifest.get('final_url')})
        if int(final[1])!=int(path[1]):reject('annual report attachment final URL issuer differs')
    expected_cik=cik_in_ref(company_ref)
    connection=core.connection
    if expected_cik is None:
        try:
            ciks={str(r[0]) for r in connection.execute('SELECT DISTINCT cik FROM coverage_mission_statement_filings WHERE company_ref=?',(company_ref,)).fetchall() if str(r[0]).isdigit()}
        except sqlite3.OperationalError:
            ciks=set()
        if len(ciks) == 1: expected_cik=next(iter(ciks))
    if (expected_cik is None or int(expected_cik) != int(parent_path[1])
            or int(child_path[1]) != int(parent_path[1])):
        reject('annual report attachment issuer CIK differs from the reviewed company')
    parent_verified,parent_rendering=verified_public_web_source(core,spool,dict(parent),exact_receipts,
        max_source_chars=max_source_chars,max_pdf_pages=max_pdf_pages,max_decompressed_bytes=max_decompressed_bytes)
    if not re.search(r'\bFORM\s+10-K(?:/A)?\b',parent_rendering['text'],re.I):
        reject('annual report attachment parent original is not an identified Form 10-K')
    primary_raw=spool.read_object(parent_verified['body_sha256'])
    name=annual_report_exhibit_name(primary_raw,accession=accession)
    if name is None or name != child_path[3] or name != outcome.get('exhibit_name'):
        reject('annual report attachment is not the 10-K original Exhibit 13 link')
    manifest,rendering=verified_public_web_source(core,spool,dict(child),exact_receipts,
        max_source_chars=max_source_chars,max_pdf_pages=max_pdf_pages,max_decompressed_bytes=max_decompressed_bytes)
    if rendering['truncated'] is not False:
        reject('annual report attachment rendering is incomplete')
    doc_ref=f'sec:exhibit:{accession}:{name}'
    binding={'schema_version':'sec-annual-report-exhibit-source-v1','document_ref':doc_ref,
        'parent_document_ref':parent_document_ref,'parent_manifest_ref':parent_verified['id'],
        'parent_manifest_hash':parent_verified['content_hash'],'parent_body_sha256':parent_verified['body_sha256'],
        'company_ref':company_ref,'cik':str(int(expected_cik)),'accession':accession,'exhibit':'13',
        'source_url':manifest['canonical_url'],'manifest_ref':manifest['id'],'manifest_hash':manifest['content_hash'],
        'fetch_document_ref':manifest['document_ref'],'body_sha256':manifest['body_sha256'],
        'raw_artifact_version_ref':manifest['raw_artifact_version_ref'],
        'source_envelope_ref':manifest['source_envelope_ref'],'source_envelope_hash':manifest['source_envelope_hash']}
    return {'document_ref':doc_ref,'manifest':manifest,'rendering':rendering,
            'binding':{**binding,'content_hash':content_hash(binding)}}
