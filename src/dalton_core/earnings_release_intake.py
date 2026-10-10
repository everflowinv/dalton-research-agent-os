"""ADR-0015: a company's own earnings release, read the day it is furnished.

A US issuer announces a quarter twice.  First an Item 2.02 8-K -- "Results of
Operations and Financial Condition" -- with the press release furnished as
Exhibit 99.1, on the day of the call.  Then, weeks later, the 10-Q or 10-K with
the XBRL lines this system has always read.  Until 2026-10-08 Dalton only knew
about the second: Accenture reported FY2026 Q4 on 2026-10-01 and a week later
the system still thought FY2026-Q3 was the newest quarter, so the Q4 call was
not even a required transcript and nothing in the Cockpit said a quarter had
been reported.

This module is the first announcement, end to end, and three rules hold
everywhere in it:

* **Preliminary is a tier, and it is the lowest one.**  Every number read out
  of a press release is a ``preliminary_actuals`` row labelled
  「初步，待 10-K/10-Q 核对」.  It is never written into ``claim_versions`` and
  never into ``forecast_reconciliations``; those stay filed-only.  The moment
  the 10-Q/10-K for that quarter is ingested the row is *superseded* -- a read
  -time fold over the statement-filing ledger, so nothing is updated -- and
  every consumer shows the filed number instead.  A preliminary number can
  therefore never outrank a filed one: there is no table both live in.
* **Deterministic, no model.**  The income-statement table is parsed out of
  the exhibit's HTML; each row carries the character span it came from, the
  accession and the exhibit body's SHA-256, and its digits are re-checked
  against that span.  A table that cannot be read unambiguously is refused
  with its reason (one ``needs_human`` card naming the filing), never guessed.
* **Fetched under the existing governance.**  The exhibit is fetched by the
  same public-web fetch child that fetched the 8-K, under the same
  ``web-fetch`` governance record, sec.gov size ceiling and mission grant, in
  the same accession directory, with its URL named by the 8-K's own exhibit
  index (``public_web_fetch_cli``).  Nothing here touches the network.

The fiscal-period half is :func:`reported_release`: from the SEC filings index
already spooled on disk, the newest Item 2.02 8-K whose fiscal quarter (by the
company's own fiscal year end) is after the newest filed 10-Q/10-K.  That
quarter is ``reported`` -- not filed -- and it becomes a required transcript
period, opens the calibration window, and names the preliminary rows.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .store import canonical_json, content_hash

SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("earnings_release_schema.sql")

PRELIMINARY_BASIS = "company-press-release-preliminary"
#: What every preliminary number is labelled with, wherever it is shown.
PRELIMINARY_LABEL = "初步，待 10-K/10-Q 核对"
EXHIBIT_NUMBER = "99.1"
EARNINGS_RELEASE_ITEM = "2.02"
#: A press release is tens of kilobytes; a bigger object is not one.
MAX_EXHIBIT_BYTES = 4_000_000
#: A release more than this long after the quarter it maps to is not about it.
RELEASE_MAX_LAG_DAYS = 100
#: How long after a release learned late its calibration window may still open.
LATE_CALIBRATION_MAX_DAYS = 45
#: How far back the spooled index is read for releases.
RELEASE_LOOKBACK_DAYS = 200
MAX_ROWS = 24
MAX_EXCERPT_CHARS = 300
READ_STATUSES = ("parsed", "unparseable", "exhibit_unavailable")
_PERIODIC_FORMS = ("10-Q", "10-K", "10-Q/A", "10-K/A")
_EXHIBIT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.(?:htm|html|txt)$")
_ACCESSION_RE = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")


class EarningsReleaseError(RuntimeError):
    """A release cannot be read the way this module reads them."""


# ---------------------------------------------------------------------------
# fiscal periods
# ---------------------------------------------------------------------------


def _period_month(value: date) -> tuple[int, int]:
    from .mission_stage import _period_month as stage_period_month

    return stage_period_month(value)


def fiscal_quarter_label(year: int, month: int, fiscal_year_end_month: int) -> str | None:
    """``FY2026-Q4`` for a quarter closing in (year, month), or None off-cycle."""

    from .mission_stage import _fiscal_quarter_of

    return _fiscal_quarter_of(date(year, month, 28), fiscal_year_end_month)


def _month_end(year: int, month: int) -> date:
    following = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    return following - timedelta(days=1)


def release_fiscal_quarter(report_date: str, fiscal_year_end_month: int) -> dict[str, Any] | None:
    """The fiscal quarter an earnings release on ``report_date`` reports.

    The newest fiscal quarter that had closed before the release: a company
    reports the quarter it has finished.  Quarter ends are the fiscal year end
    month and every third month before it (Accenture: Aug, May, Feb, Nov;
    Microsoft: Jun, Mar, Dec, Sep).  A 52/53-week quarter that ends on the
    2nd closes the previous month, the same rule ``mission_stage`` uses.
    """

    day = date.fromisoformat(str(report_date)[:10])
    if not 1 <= int(fiscal_year_end_month) <= 12:
        return None
    quarter_months = {((int(fiscal_year_end_month) - 1 - 3 * k) % 12) + 1 for k in range(4)}
    year, month = day.year, day.month
    for _ in range(4):
        month -= 1
        if month == 0:
            year, month = year - 1, 12
        if month in quarter_months:
            end = _month_end(year, month)
            if (day - end).days > RELEASE_MAX_LAG_DAYS:
                return None
            label = fiscal_quarter_label(year, month, int(fiscal_year_end_month))
            if label is None:
                return None
            start_year, start_month = (year, month - 2) if month > 2 else (year - 1, month + 10)
            return {"period": label, "period_end": end.isoformat(),
                    "period_start": date(start_year, start_month, 1).isoformat()}
    return None


def _quarter_ordinal(period: str) -> int:
    from .mission_stage import _quarter_ordinal as ordinal

    return ordinal(period)


def core_state_dir(connection: sqlite3.Connection) -> Path | None:
    """The directory of the Core this connection has open, or None in memory."""

    try:
        row = connection.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error:
        return None
    path = row[2] if row is not None and len(row) > 2 else None
    if not path:
        return None
    if path.startswith("file:"):
        path = path[5:].split("?", 1)[0]
    return Path(path).parent


def _statement_filings(connection: sqlite3.Connection, company_ref: str) -> list[dict[str, Any]]:
    try:
        rows = connection.execute(
            "SELECT company_ref,cik,accession,form,filed,report_date,ingest_id "
            "FROM coverage_mission_statement_filings WHERE company_ref=?", (company_ref,),
        ).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for row in rows:
        try:
            day = date.fromisoformat(str(row[5])[:10])
        except ValueError:
            continue
        out.append({"company_ref": row[0], "cik": str(row[1]), "accession": row[2],
                    "form": str(row[3]), "filed": row[4], "report_date": day,
                    "ingest_id": row[6]})
    return out


def fiscal_year_end_month(
    connection: sqlite3.Connection, company_ref: str, *, payload: Mapping[str, Any] | None = None,
) -> int | None:
    """The month a company's fiscal year ends in.

    The newest filed 10-K's period end first (what ``mission_stage`` uses),
    then SEC's own ``fiscalYearEnd`` (``"0831"``) from the spooled index.
    """

    annual = [row["report_date"] for row in _statement_filings(connection, company_ref)
              if row["form"].startswith("10-K")]
    if annual:
        return _period_month(max(annual))[1]
    stated = (payload or {}).get("fiscalYearEnd")
    if isinstance(stated, str) and re.fullmatch(r"(0[1-9]|1[0-2])[0-3][0-9]", stated):
        return int(stated[:2])
    return None


def latest_filed_quarter(connection: sqlite3.Connection, company_ref: str,
                         fye_month: int | None) -> dict[str, Any] | None:
    held = [row for row in _statement_filings(connection, company_ref)
            if row["form"] in _PERIODIC_FORMS]
    if not held or fye_month is None:
        return None
    newest = max(held, key=lambda row: row["report_date"])
    year, month = _period_month(newest["report_date"])
    label = fiscal_quarter_label(year, month, fye_month)
    if label is None:
        return None
    return {"period": label, "report_date": newest["report_date"].isoformat(),
            "accession": newest["accession"], "form": newest["form"]}


def filed_quarters(connection: sqlite3.Connection, company_ref: str,
                   fye_month: int | None) -> dict[str, dict[str, Any]]:
    """Every fiscal quarter a 10-Q/10-K has been ingested for, by label."""

    out: dict[str, dict[str, Any]] = {}
    if fye_month is None:
        return out
    for row in _statement_filings(connection, company_ref):
        if row["form"] not in _PERIODIC_FORMS:
            continue
        year, month = _period_month(row["report_date"])
        label = fiscal_quarter_label(year, month, fye_month)
        if label is not None and label not in out:
            out[label] = {"accession": row["accession"], "form": row["form"],
                          "report_date": row["report_date"].isoformat(),
                          "ingest_id": row["ingest_id"]}
    return out


def issuer_cik(connection: sqlite3.Connection, company_ref: str,
               state_dir: Path | None) -> str | None:
    ciks = {row["cik"].lstrip("0") for row in _statement_filings(connection, company_ref)}
    if len(ciks) == 1:
        return next(iter(ciks)).zfill(10)
    from .mission_company_cik import company_cik

    cik = company_cik(company_ref, state_dir=state_dir)
    return None if cik is None else str(cik).lstrip("0").zfill(10)


# The spooled index is read at most once per artifact per process: the
# cockpit and the stage evaluator ask about every company on every refresh.
_INDEX_CACHE: dict[str, dict[str, Any]] = {}
_INDEX_CACHE_MAX = 64
_ELIGIBLE_CACHE: dict[str, set[str]] = {}


def _index_releases(connection: sqlite3.Connection, state_dir: Path,
                    issuer: str) -> dict[str, Any]:
    from .sec_earnings_release import (
        SecEarningsReleaseError, earnings_release_filings, read_submissions_artifact,
        submissions_artifacts,
    )

    for artifact in submissions_artifacts(connection, issuer=issuer):
        digest = artifact["artifact_hash"]
        cached = _INDEX_CACHE.get(digest)
        if cached is not None:
            return {**cached, **artifact}
        found = read_submissions_artifact(state_dir, digest)
        if found["status"] == "absent":
            continue
        if found["status"] != "read":
            return {"status": found["status"], "reason": found.get("reason"),
                    "releases": [], **artifact}
        try:
            releases = earnings_release_filings(found["payload"], limit=40)
        except SecEarningsReleaseError as exc:
            return {"status": "unreadable", "reason": str(exc), "releases": [], **artifact}
        value = {"status": "read", "releases": releases,
                 "fiscal_year_end": found["payload"].get("fiscalYearEnd")}
        if len(_INDEX_CACHE) >= _INDEX_CACHE_MAX:
            _INDEX_CACHE.pop(next(iter(_INDEX_CACHE)))
        _INDEX_CACHE[digest] = value
        return {**value, **artifact}
    return {"status": "unavailable", "releases": [],
            "reason": f"no spooled SEC filings index for issuer {issuer}"}


def releases_with_periods(
    connection: sqlite3.Connection, company_ref: str, *,
    state_dir: Path | None = None, today: str | None = None,
) -> dict[str, Any]:
    """Every Item 2.02 release in the spooled index, each with its fiscal quarter."""

    state = state_dir if state_dir is not None else core_state_dir(connection)
    if state is None:
        return {"status": "unavailable", "reason": "in-memory Core", "releases": []}
    issuer = issuer_cik(connection, company_ref, state)
    if issuer is None:
        return {"status": "unavailable", "reason": "no SEC CIK for this company", "releases": []}
    index = _index_releases(connection, state, issuer)
    if index["status"] != "read":
        return {**index, "issuer": issuer}
    fye = fiscal_year_end_month(connection, company_ref,
                                payload={"fiscalYearEnd": index.get("fiscal_year_end")})
    since = None
    if today is not None:
        since = (date.fromisoformat(today) - timedelta(days=RELEASE_LOOKBACK_DAYS)).isoformat()
    releases = []
    for release in index["releases"]:
        if since is not None and release["report_date"] < since:
            continue
        mapped = None if fye is None else release_fiscal_quarter(release["report_date"], fye)
        releases.append({**release, **(mapped or {"period": None}),
                         "invocation_ref": index.get("invocation_ref"),
                         "artifact_hash": index.get("artifact_hash")})
    return {"status": "read", "issuer": issuer, "fiscal_year_end_month": fye,
            "releases": releases, "invocation_ref": index.get("invocation_ref"),
            "artifact_hash": index.get("artifact_hash")}


def reported_release(
    connection: sqlite3.Connection, company_ref: str, *,
    state_dir: Path | None = None, today: str | None = None,
) -> dict[str, Any] | None:
    """The just-reported, not-yet-filed quarter, or None.

    The newest Item 2.02 release whose fiscal quarter is after the newest
    filed 10-Q/10-K quarter.  ``status`` is ``reported``; the quarter becomes
    ``filed`` -- and this returns None -- the day its 10-Q/10-K is ingested.
    """

    found = releases_with_periods(connection, company_ref, state_dir=state_dir, today=today)
    if found["status"] != "read" or found.get("fiscal_year_end_month") is None:
        return None
    fye = found["fiscal_year_end_month"]
    filed = latest_filed_quarter(connection, company_ref, fye)
    best = None
    for release in found["releases"]:
        if not release.get("period"):
            continue
        if filed is not None and _quarter_ordinal(release["period"]) <= _quarter_ordinal(filed["period"]):
            continue
        if best is None or (release["report_date"], release["accession"]) > (
                best["report_date"], best["accession"]):
            best = release
    if best is None:
        return None
    return {**best, "status": "reported", "company_ref": company_ref,
            "fiscal_year_end_month": fye,
            "latest_filed_period": None if filed is None else filed["period"]}


def reported_fiscal_quarters(
    connection: sqlite3.Connection, company_refs: Sequence[str], *,
    state_dir: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """company_ref -> its reported-not-filed release, for those that have one."""

    state = state_dir if state_dir is not None else core_state_dir(connection)
    if state is None:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for company_ref in company_refs:
        try:
            found = reported_release(connection, company_ref, state_dir=state)
        except (sqlite3.Error, OSError, ValueError, EarningsReleaseError):
            continue
        if found is not None:
            out[company_ref] = found
    return out


# ---------------------------------------------------------------------------
# the exhibit: which file, at which URL
# ---------------------------------------------------------------------------


_HREF_RE = re.compile(r"""<a\b[^>]*?\bhref\s*=\s*["']([^"'#?]+)["'][^>]*>(.*?)</a>""",
                      re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_EX991_NAME_RE = re.compile(r"(?:ex|exhibit|dex)[-_]?99[-_.]?0?1(?![0-9])", re.IGNORECASE)


def _plain_text(fragment: str) -> str:
    return re.sub(r"\s+", " ", unescape(_TAG_RE.sub(" ", fragment))).replace("\xa0", " ").strip()


#: 2026-10-09: which detector found (or failed to find) an exhibit.  A fetch
#: summary whose exhibit failed under an older detector is re-detected once
#: (``exhibit_redetect_due``).  ``filing-index:0.2`` reads the filing's own
#: document table first; ``primary-links:0.1`` only read the 8-K's links.
EXHIBIT_DETECTOR_VERSION = "filing-index:0.2"
#: The failure that means "we could not tell which file it is" -- the only
#: one a better detector can fix.  Network or size failures are not retried
#: by re-detection.
EXHIBIT_NOT_FOUND_REASON = "8-K 的附件目录里找不到附件 99.1 的链接"
EXHIBIT_REDETECT_FILE = "exhibit-redetect.json"
_PRESS_RELEASE_WORDS = re.compile(
    r"press\s+release|news\s+release|earnings\s+release|results", re.IGNORECASE)


def filing_index_name(accession: str) -> str:
    """The filing's own index page in its accession directory (``…-index.html``)."""

    if not isinstance(accession, str) or _ACCESSION_RE.fullmatch(accession) is None:
        raise EarningsReleaseError("accession must be an EDGAR accession")
    return f"{accession}-index.html"


def _cells(row: str) -> list[str]:
    return [_plain_text(cell) for cell in re.findall(
        r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row, re.IGNORECASE | re.DOTALL)]


def exhibit_name_from_filing_index(index_html: str | bytes) -> str | None:
    """Exhibit 99.1's file name from the filing's own document-type table.

    EDGAR's ``<accession>-index.html`` lists every document with the *type*
    the filer declared (``EX-99.1``, ``EX-99``, ``GRAPHIC``…) whatever the file
    is called.  Live 2026-10-08: Accenture's FY26 Q4 release is
    ``q4fy26earnings8-kexhibit.htm``, typed ``EX-99`` and listed in the 8-K as
    exhibit "99" -- no name or link text says 99.1, so the link heuristic
    found nothing.  Rule: the one document typed ``EX-99.1``; else the only
    ``EX-99``/``EX-99.x`` document; else the only one of those whose
    description reads like a press / news / earnings release.  A name with a
    path, or anything ambiguous, is ``None``.
    """

    text = index_html.decode("utf-8", "replace") if isinstance(index_html, bytes) else str(index_html)
    table = re.search(r"<table\b[^>]*summary=[\"']Document Format Files[\"'][^>]*>(.*?)</table>",
                      text, re.IGNORECASE | re.DOTALL)
    body = table.group(1) if table else text
    rows: list[tuple[str, str, str]] = []
    for row in re.findall(r"<tr\b.*?</tr>", body, re.IGNORECASE | re.DOTALL):
        cells = _cells(row)
        hrefs = [match.group(1).strip() for match in _HREF_RE.finditer(row)]
        if len(cells) < 4 or not hrefs:
            continue
        kind = cells[3].strip().upper()
        if not kind.startswith("EX-99"):
            continue
        href = hrefs[0]
        if "?" in href or "#" in href:
            continue
        name = href.rsplit("/", 1)[-1]
        if _EXHIBIT_NAME_RE.fullmatch(name) is None:
            continue
        rows.append((kind, name, cells[1]))
    exact = [name for kind, name, _ in rows if kind in {"EX-99.1", "EX-99.01"}]
    if len(set(exact)) == 1:
        return exact[0]
    if exact:
        return None
    names = list(dict.fromkeys(name for _, name, _ in rows))
    if len(names) == 1:
        return names[0]
    described = list(dict.fromkeys(name for _, name, words in rows
                                   if _PRESS_RELEASE_WORDS.search(words)))
    return described[0] if len(described) == 1 else None


def exhibit_redetect_due(state_dir: Path, ticket_ref: str) -> bool:
    """Should this 8-K fetch's exhibit be looked for again with the current detector?

    Only when the exhibit failed because no file could be identified, and no
    re-detection under this detector version has been recorded.  Read-only.
    """

    if not isinstance(ticket_ref, str) or ":" not in ticket_ref:
        return False
    directory = Path(state_dir) / "fetches" / ticket_ref.split(":", 1)[1]
    summary = _owner_json(directory / "summary.json")
    if summary is None or summary.get("status") != "succeeded":
        return False
    exhibit = summary.get("exhibit")
    if not isinstance(exhibit, Mapping) or exhibit.get("status") == "succeeded":
        return False
    if exhibit.get("failure_reason") != EXHIBIT_NOT_FOUND_REASON:
        return False
    redetect = _owner_json(directory / EXHIBIT_REDETECT_FILE)
    return not (isinstance(redetect, Mapping)
                and redetect.get("detector_version") == EXHIBIT_DETECTOR_VERSION)


def exhibit_redetect_candidates(connection: sqlite3.Connection, state_dir: Path,
                                *, limit: int = 1) -> list[dict[str, Any]]:
    """Acquired Item 2.02 8-Ks whose exhibit an older detector could not find."""

    if not _table_exists(connection, "earnings_release_reads"):
        return []
    out: list[dict[str, Any]] = []
    for read in failed_reads(connection):
        if read.get("status") != "exhibit_unavailable" or not read.get("ticket_ref"):
            continue
        if exhibit_redetect_due(state_dir, str(read["ticket_ref"])):
            out.append({"company_ref": read["company_ref"], "accession": read["accession"],
                        "ticket_ref": read["ticket_ref"], "document_ref": read.get("document_ref")})
        if len(out) >= limit:
            break
    return out


def press_release_exhibit_name(primary_html: str | bytes) -> str | None:
    """The file name of Exhibit 99.1, from the 8-K's own exhibit index.

    Since 2017 an 8-K hyperlinks each exhibit in its exhibit index, so the
    name comes out of the filing the lane already fetched: the link whose row
    (or link text) says 99.1, else the one link whose file name spells it
    (``msft-ex99_1.htm``, ``d123dex991.htm``).  A relative name in the same
    directory only -- a link anywhere else is not this filing's exhibit.
    """

    text = primary_html.decode("utf-8", "replace") if isinstance(primary_html, bytes) else str(primary_html)
    candidates: list[str] = []
    numbered: list[str] = []
    for row in re.finditer(r"<tr\b.*?</tr>", text, re.IGNORECASE | re.DOTALL):
        body = row.group(0)
        if not re.search(r"(?<![0-9.])99\.1(?![0-9])", _plain_text(body)):
            # 2026-10-09: an exhibit index that numbers the release plain
            # "99" (Accenture's 8-Ks) -- its first cell is the number.
            first = next((cell for cell in _cells(body) if cell), "")
            if first in {"99", "99.01"}:
                numbered.extend(match.group(1).strip() for match in _HREF_RE.finditer(body))
            continue
        for match in _HREF_RE.finditer(body):
            candidates.append(match.group(1).strip())
    if not candidates:
        candidates = numbered
    if not candidates:
        for match in _HREF_RE.finditer(text):
            href = match.group(1).strip()
            if (re.search(r"(?<![0-9.])99\.1(?![0-9])", _plain_text(match.group(2)))
                    or _EX991_NAME_RE.search(href.rsplit("/", 1)[-1])):
                candidates.append(href)
    names: list[str] = []
    for href in candidates:
        if "://" in href and not href.startswith("https://www.sec.gov/Archives/edgar/data/"):
            continue
        if "://" not in href and "/" in href:
            continue
        name = href.rsplit("/", 1)[-1]
        if _EXHIBIT_NAME_RE.fullmatch(name) and name not in names:
            names.append(name)
    if len(names) == 1:
        return names[0]
    spelled = [name for name in names if _EX991_NAME_RE.search(name)]
    return spelled[0] if len(spelled) == 1 else None


def exhibit_url(issuer: str, accession: str, name: str) -> str:
    """The exhibit's EDGAR archive URL, in the accession's own directory."""

    if not isinstance(name, str) or _EXHIBIT_NAME_RE.fullmatch(name) is None:
        raise EarningsReleaseError("exhibit file name is not a plain document name")
    if not isinstance(accession, str) or _ACCESSION_RE.fullmatch(accession) is None:
        raise EarningsReleaseError("accession must be an EDGAR accession")
    if not str(issuer).strip("0").isdigit():
        raise EarningsReleaseError("issuer must be a numeric CIK")
    return (f"https://www.sec.gov/Archives/edgar/data/{int(issuer)}/"
            f"{accession.replace('-', '')}/{name}")


def ineligible_filing_refs(connection: sqlite3.Connection, plan: Mapping[str, Any],
                           *, limit: int = 400) -> tuple[str, ...]:
    """Discovered filings a code-owned item filter says not to fetch.

    For each spec whose code declares ``item_filter`` (the earnings-release
    8-K: Item 2.02), every queued or failed filing of that spec whose
    submissions row does not carry the item.  Read from the exact index bytes
    the discovery was answered from; an index that cannot be read excludes
    nothing (the fetch child checks again before it fetches an exhibit).
    """

    from .sec_earnings_release import earnings_release_filings, read_submissions_artifact
    from .sec_plan_specs import acquisition_item_filter

    filters = {spec["spec_ref"]: acquisition_item_filter(spec["spec_ref"])
               for spec in plan.get("specs") or ()}
    filters = {ref: items for ref, items in filters.items() if items}
    state = core_state_dir(connection)
    if not filters or state is None:
        return ()
    try:
        rows = connection.execute(
            "SELECT d.document_ref, s.spec_ref, e.record_json, d.company_ref "
            "FROM coverage_mission_discovered_documents d "
            "JOIN coverage_mission_source_discoveries s ON s.record_id=d.discovery_ref "
            "JOIN connector_source_envelopes e ON e.source_envelope_id=s.source_envelope_ref "
            "WHERE d.source_ref='source:sec-edgar' AND d.status IN ('discovered','acquisition_failed') "
            f"AND s.spec_ref IN ({','.join('?' * len(filters))}) LIMIT ?",
            (*filters, int(limit))).fetchall()
    except sqlite3.Error:
        return ()
    eligible: dict[str, set[str] | None] = {}
    already_filed: dict[str, set[str]] = {}
    out: list[str] = []
    for document_ref, spec_ref, envelope_json, company_ref in rows:
        try:
            digest = json.loads(envelope_json)["raw_response_hash"]
        except (TypeError, ValueError, KeyError):
            continue
        key = f"{digest}|{spec_ref}"
        if key not in eligible and key in _ELIGIBLE_CACHE:
            eligible[key] = _ELIGIBLE_CACHE[key]
        if key not in eligible:
            found = read_submissions_artifact(state, digest)
            if found["status"] != "read":
                eligible[key] = None
            else:
                items = set(filters[spec_ref])
                try:
                    eligible[key] = {
                        f"sec:filing:{release['accession']}"
                        for release in earnings_release_filings(found["payload"], item=next(iter(items)),
                                                                limit=1000)}
                except Exception:  # noqa: BLE001
                    eligible[key] = None
                else:
                    # Content-addressed: the same digest always answers the same.
                    if len(_ELIGIBLE_CACHE) >= _INDEX_CACHE_MAX * 4:
                        _ELIGIBLE_CACHE.pop(next(iter(_ELIGIBLE_CACHE)))
                    _ELIGIBLE_CACHE[key] = eligible[key]
        allowed = eligible[key]
        if allowed is None or document_ref in out:
            continue
        if document_ref not in allowed:
            out.append(document_ref)
            continue
        # A release whose quarter the 10-Q/10-K already covers would only be
        # read to be superseded: the filed numbers are what every reader uses.
        if company_ref not in already_filed:
            already_filed[company_ref] = _filed_release_refs(connection, company_ref, state)
        if document_ref in already_filed[company_ref]:
            out.append(document_ref)
    return tuple(out)


def _filed_release_refs(connection: sqlite3.Connection, company_ref: str, state: Path) -> set[str]:
    try:
        found = releases_with_periods(connection, company_ref, state_dir=state)
        fye = found.get("fiscal_year_end_month")
        filed = filed_quarters(connection, company_ref, fye)
    except Exception:  # noqa: BLE001 - unknown means fetch it
        return set()
    return {f"sec:filing:{release['accession']}" for release in found.get("releases") or ()
            if release.get("period") and release["period"] in filed}


# ---------------------------------------------------------------------------
# the parser
# ---------------------------------------------------------------------------


class _Tables(HTMLParser):
    """Every table's rows as (start, end, cells), with char offsets into the text."""

    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=True)
        self._line_starts = [0]
        for index, char in enumerate(text):
            if char == "\n":
                self._line_starts.append(index + 1)
        self.tables: list[dict[str, Any]] = []
        self._stack: list[dict[str, Any]] = []
        self._row: dict[str, Any] | None = None
        self._cell: list[str] | None = None
        self._text_tail: list[str] = []

    def _offset(self) -> int:
        line, column = self.getpos()
        return self._line_starts[line - 1] + column

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        tag = tag.lower()
        if tag == "table":
            before = re.sub(r"\s+", " ", "".join(self._text_tail)).strip()[-800:]
            table = {"rows": [], "before": before, "start": self._offset()}
            self._stack.append(table)
            self.tables.append(table)
        elif tag == "tr" and self._stack:
            self._row = {"start": self._offset(), "cells": []}
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag in ("br", "p", "div") and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row["cells"].append(re.sub(r"\s+", " ", "".join(self._cell).replace("\xa0", " ")).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None and self._stack:
            line, column = self.getpos()
            self._row["end"] = self._line_starts[line - 1] + column + len("</tr>")
            self._stack[-1]["rows"].append(self._row)
            self._row = None
        elif tag == "table" and self._stack:
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)
        elif not self._stack:
            self._text_tail.append(data)
            if len(self._text_tail) > 400:
                self._text_tail = self._text_tail[-200:]


_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december")
_DATE_RE = re.compile(r"\b(" + "|".join(_MONTHS) + r")\s+([0-9]{1,2})\s*,?\s*((?:19|20)[0-9]{2})?",
                      re.IGNORECASE)
_YEAR_RE = re.compile(r"(?<![0-9])((?:19|20)[0-9]{2})(?![0-9])")
_NUMBER_TOKEN_RE = re.compile(
    r"(\()?\s*\$?\s*(\()?\s*(-|−)?([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(\.[0-9]+)?\s*(\))?\s*(%)?"
    r"|(?<![A-Za-z0-9])([—–-])(?![A-Za-z0-9])")
_QUARTER_HEADER_RE = re.compile(r"three\s+months\s+ended|quarter\s+ended|fourth\s+quarter|"
                                r"first\s+quarter|second\s+quarter|third\s+quarter|13\s+weeks\s+ended",
                                re.IGNORECASE)

# metric -> (label patterns, kind).  ``kind`` decides the unit: money in the
# table's scale, a per-share amount in dollars, a share count.
_METRIC_PATTERNS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("revenue", (r"^(total )?(net )?revenues?(, net)?$", r"^revenues?, net$", r"^(total )?net sales$",
                 r"^total revenues? and other income$"), "money"),
    ("cost_of_revenue", (r"^(total )?cost of (services|revenues?|sales)$",), "money"),
    ("gross_profit", (r"^gross (margin|profit)$",), "money"),
    ("operating_income", (r"^(total )?operating income( \(loss\))?$", r"^income from operations$",
                          r"^operating (income|profit)$"), "money"),
    ("pretax_income", (r"^income before (provision for )?income tax(es)?( expense)?$",
                       r"^income before taxes$"), "money"),
    ("income_tax", (r"^(provision for )?income tax(es)?( expense)?$", r"^provision for income taxes$"),
     "money"),
    ("net_income", (r"^net income( \(loss\))?$", r"^net earnings$"), "money"),
    ("net_income_attributable", (r"^net income attributable to (?!non)(?!.*noncontrolling).+$",),
     "money"),
    ("dividends_per_share", (r"^cash dividends (declared )?per (ordinary )?share$",
                             r"^dividends (declared )?per share$"), "per_share"),
)
_EPS_SECTION_RE = re.compile(r"per (ordinary |common |class a ordinary )?share|earnings per", re.IGNORECASE)
_SHARES_SECTION_RE = re.compile(r"weighted[- ]average|shares outstanding|shares used", re.IGNORECASE)

METRIC_LABELS: Mapping[str, str] = {
    "revenue": "营业收入", "cost_of_revenue": "营业成本", "gross_profit": "毛利",
    "operating_income": "营业利润", "pretax_income": "税前利润", "income_tax": "所得税",
    "net_income": "净利润", "net_income_attributable": "归属于公司的净利润",
    "eps_basic": "基本每股收益", "eps_diluted": "稀释每股收益",
    "shares_basic": "基本加权平均股数", "shares_diluted": "稀释加权平均股数",
    "dividends_per_share": "每股现金股利",
}
#: The XBRL concepts a filed line carries the same number under, so a
#: superseded preliminary row can show the filed figure beside it.
METRIC_CONCEPTS: Mapping[str, tuple[str, ...]] = {
    "revenue": ("us-gaap:Revenues", "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                "us-gaap:SalesRevenueNet"),
    "operating_income": ("us-gaap:OperatingIncomeLoss",),
    "net_income": ("us-gaap:ProfitLoss", "us-gaap:NetIncomeLoss"),
    "net_income_attributable": ("us-gaap:NetIncomeLoss",),
    "eps_basic": ("us-gaap:EarningsPerShareBasic",),
    "eps_diluted": ("us-gaap:EarningsPerShareDiluted",),
    "pretax_income": ("us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",),
    "income_tax": ("us-gaap:IncomeTaxExpenseBenefit",),
    "cost_of_revenue": ("us-gaap:CostOfRevenue", "us-gaap:CostOfServices"),
}
SCALES: Mapping[str, Decimal] = {"one": Decimal(1), "thousand": Decimal(10) ** 3,
                                 "million": Decimal(10) ** 6, "billion": Decimal(10) ** 9}


def _clean_label(label: str) -> str:
    text = re.sub(r"\(\s*[0-9a-z]\s*\)\s*$", "", label.strip(), flags=re.IGNORECASE)
    text = re.sub(r"[:\s]+$", "", text)
    text = text.replace("—", "-").replace("–", "-").replace("’", "'")
    return re.sub(r"\s+", " ", text).strip().casefold()


def _row_tokens(cells: Sequence[str]) -> tuple[str, list[dict[str, Any]]]:
    """(label, value tokens) of one row: the first texty cell, then every number."""

    label = ""
    rest: list[str] = []
    for index, cell in enumerate(cells):
        if not label and re.search(r"[A-Za-z]{2,}", cell):
            label = cell
            rest = list(cells[index + 1:])
            break
    joined = " ".join(cell for cell in rest if cell)
    tokens: list[dict[str, Any]] = []
    for match in _NUMBER_TOKEN_RE.finditer(joined):
        if match.group(8):
            tokens.append({"nil": True, "percent": False, "text": match.group(8)})
            continue
        if match.group(4) is None:
            continue
        digits = match.group(4).replace(",", "") + (match.group(5) or "")
        negative = bool(match.group(3)) or (
            bool(match.group(1) or match.group(2)) and bool(match.group(6)))
        tokens.append({"nil": False, "percent": bool(match.group(7)),
                       "value": ("-" if negative else "") + digits, "text": match.group(0).strip()})
    # A "%" in its own cell follows the number it qualifies.
    return label, tokens


def _canonical(value: str) -> str:
    """The number as written (``3.03``, ``17595123``), sign kept, no exponent."""

    text = format(Decimal(value), "f")
    return "0" if text in ("-0", "") else text


def _scale_of(text: str) -> str | None:
    lowered = text.casefold()
    for word, scale in (("in billions", "billion"), ("in millions", "million"),
                        ("in thousands", "thousand")):
        if word in lowered:
            return scale
    match = re.search(r"\$\s*in\s+(billions|millions|thousands)", lowered)
    if match:
        return {"billions": "billion", "millions": "million", "thousands": "thousand"}[match.group(1)]
    return None


def _header_period_end(header: str) -> tuple[date | None, list[int]]:
    """The first period end the header names after its quarter heading, and its years."""

    quarter = _QUARTER_HEADER_RE.search(header)
    tail = header[quarter.start():] if quarter else header
    years = [int(year) for year in _YEAR_RE.findall(tail)]
    for match in _DATE_RE.finditer(tail):
        month = _MONTHS.index(match.group(1).casefold()) + 1
        year = int(match.group(3)) if match.group(3) else (years[0] if years else None)
        if year is None:
            continue
        try:
            return date(year, month, int(match.group(2))), years
        except ValueError:
            continue
    return None, years


def parse_press_release(
    raw: bytes | str, *, expected_period_end: str | None = None,
) -> dict[str, Any]:
    """The income statement's current-quarter column, row by row.

    Returns ``{"status": "parsed", "rows": [...], "table": {...}}`` or
    ``{"status": "unparseable", "reason": "<中文原因>"}``.  Every row carries
    ``span`` -- the character offsets of its ``<tr>`` in the decoded exhibit
    -- and ``excerpt``, and its value is re-found in that span's text.
    """

    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    if raw is None or not text.strip():
        return {"status": "unparseable", "reason": "附件 99.1 是空的"}
    parser = _Tables(text)
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - malformed HTML is a refusal, not a crash
        return {"status": "unparseable", "reason": f"附件 99.1 的 HTML 无法解析：{type(exc).__name__}"}
    tried: list[str] = []
    for index, table in enumerate(parser.tables):
        rows = table["rows"]
        parsed_rows = [(row, *_row_tokens(row["cells"])) for row in rows]
        labels = [_clean_label(label) for _row, label, _tokens in parsed_rows]
        if not any(re.fullmatch(p, label) for label in labels for p in _METRIC_PATTERNS[0][1]):
            continue
        if not any(re.fullmatch(p, label) for label in labels
                   for name, patterns, _k in _METRIC_PATTERNS
                   if name in ("net_income", "operating_income", "net_income_attributable")
                   for p in patterns):
            continue
        first_data = next((i for i, (_r, label, tokens) in enumerate(parsed_rows)
                           if any(not t["nil"] and not t["percent"] for t in tokens) and label
                           and not _YEAR_RE.search(label) and not _DATE_RE.search(label)
                           and not _QUARTER_HEADER_RE.search(label)), None)
        header_rows = parsed_rows[:first_data] if first_data is not None else parsed_rows
        header = " ".join(" ".join(row["cells"]) for row, _l, _t in header_rows)
        context = f"{table['before']} {header}"
        if not _QUARTER_HEADER_RE.search(header) and not _QUARTER_HEADER_RE.search(table["before"][-300:]):
            tried.append(f"第 {index + 1} 张表没有“季度”列（Three Months Ended）")
            continue
        period_end, years = _header_period_end(header if _QUARTER_HEADER_RE.search(header) else context)
        if period_end is None:
            tried.append(f"第 {index + 1} 张表的表头没写本季截止日")
            continue
        if len(years) >= 2 and years[0] < years[1]:
            tried.append(f"第 {index + 1} 张表把上年列放在前面，不确定哪列是本季")
            continue
        scale = _scale_of(context) or _scale_of(" ".join(" ".join(r["cells"]) for r in rows[:6]))
        if scale is None:
            tried.append(f"第 {index + 1} 张表没写金额单位（千/百万）")
            continue
        all_text = context + " " + " ".join(" ".join(r["cells"]) for r in rows)
        if "$" not in all_text and "dollar" not in all_text.casefold():
            tried.append(f"第 {index + 1} 张表不是美元报表")
            continue
        shares_scale = "one" if re.search(r"except\s+(for\s+)?share", context, re.IGNORECASE) else scale
        if expected_period_end is not None:
            expected = date.fromisoformat(expected_period_end)
            if _period_month(period_end) != _period_month(expected):
                return {"status": "unparseable",
                        "reason": (f"附件表头的本季截止日 {period_end.isoformat()} 与 8-K 对应的"
                                   f"财季（截至 {expected_period_end}）不符，未读取")}
        found: dict[str, dict[str, Any]] = {}
        section = ""
        for row, label, tokens in parsed_rows[first_data or 0:]:
            clean = _clean_label(label)
            values = [t for t in tokens if not t["percent"]]
            if not values:
                if clean:
                    section = clean
                continue
            metric = None
            kind = "money"
            for name, patterns, metric_kind in _METRIC_PATTERNS:
                if any(re.fullmatch(p, clean) for p in patterns):
                    metric, kind = name, metric_kind
                    break
            if metric is None:
                if re.fullmatch(r"(basic|diluted)( earnings per (ordinary |common )?share)?", clean) or re.fullmatch(
                        r"(basic|diluted) (net income|earnings) per (ordinary |common |class a ordinary )?share", clean):
                    word = clean.split()[0]
                    if _EPS_SECTION_RE.search(section) or "per" in clean:
                        metric, kind = f"eps_{word}", "per_share"
                    elif _SHARES_SECTION_RE.search(section):
                        metric, kind = f"shares_{word}", "shares"
                elif re.fullmatch(r"(earnings|net income) per (ordinary |common )?share[ -]+(basic|diluted)", clean):
                    metric, kind = f"eps_{clean.rsplit(' ', 1)[-1].strip('- ')}", "per_share"
            if metric is None or metric in found:
                continue
            first = values[0]
            if first["nil"]:
                continue
            value = _canonical(first["value"])
            # The row as a reader sees it, cell by cell: the verbatim check
            # runs on this, and the span says where it sits in the bytes.
            row_text = " | ".join(cell for cell in row["cells"] if cell)
            from .document_numeric_claim import numbers_in

            if abs(Decimal(value)) not in {abs(n) for n in numbers_in(row_text)}:
                continue
            unit, row_scale = {"money": ("USD", scale), "per_share": ("USD/share", "one"),
                               "shares": ("shares", shares_scale)}[kind]
            found[metric] = {
                "metric": metric, "label_as_filed": label.strip(), "label": METRIC_LABELS.get(metric, metric),
                "value": value, "unit": unit, "scale": row_scale,
                "span": [row["start"], row["end"]],
                "excerpt": row_text[:MAX_EXCERPT_CHARS],
                "excerpt_hash": hashlib.sha256(row_text.encode("utf-8")).hexdigest(),
            }
        if "revenue" not in found:
            tried.append(f"第 {index + 1} 张表找不到营业收入行的本季数字")
            continue
        ordered = [found[name] for name in (
            "revenue", "cost_of_revenue", "gross_profit", "operating_income", "pretax_income",
            "income_tax", "net_income", "net_income_attributable", "eps_basic", "eps_diluted",
            "shares_basic", "shares_diluted", "dividends_per_share") if name in found][:MAX_ROWS]
        return {
            "status": "parsed", "rows": ordered,
            "table": {"index": index, "period_end": period_end.isoformat(), "scale": scale,
                      "shares_scale": shares_scale, "currency": "USD",
                      "header": header[:400], "span": [table["start"], rows[-1]["end"]]},
        }
    reason = "附件 99.1 里找不到可以确定读取的利润表（季度列、单位、营业收入和利润行）"
    if tried:
        reason += "：" + "；".join(tried[:3])
    return {"status": "unparseable", "reason": reason}


# ---------------------------------------------------------------------------
# the fetched exhibit, read back
# ---------------------------------------------------------------------------


def _owner_json(path: Path) -> dict[str, Any] | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 2_000_000:
                return None
            value = json.loads(handle.read(2_000_001).decode("utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def exhibit_for_ticket(state_dir: Path, ticket_ref: str, document_ref: str) -> dict[str, Any]:
    """What one 8-K fetch ticket says about its Exhibit 99.1.

    ``{"status": "fetched", "manifest": ..., "bytes": ...}`` with the body
    re-hashed against the manifest, or a status and a reason.
    """

    if not isinstance(ticket_ref, str) or ":" not in ticket_ref:
        return {"status": "no_ticket", "reason": "这份 8-K 没有抓取记录"}
    directory = Path(state_dir) / "fetches" / ticket_ref.split(":", 1)[1]
    summary = _owner_json(directory / "summary.json")
    if summary is None or summary.get("status") != "succeeded" or summary.get("url_ref") != document_ref:
        return {"status": "no_ticket", "reason": "这份 8-K 的抓取记录不完整"}
    exhibit = summary.get("exhibit")
    if not isinstance(exhibit, Mapping):
        return {"status": "not_attempted",
                "reason": "这份 8-K 抓取时还没有附件读取（代码上线前取回的）"}
    # 2026-10-09: a later re-detection of the same 8-K's exhibit (the filing's
    # document table, see ``exhibit_name_from_filing_index``) supersedes the
    # fetch-time attempt; its manifest is written beside the 8-K's.
    redetect = _owner_json(directory / EXHIBIT_REDETECT_FILE)
    if (isinstance(redetect, Mapping) and redetect.get("ticket_ref") == ticket_ref
            and isinstance(redetect.get("exhibit"), Mapping)):
        exhibit = redetect["exhibit"]
    if exhibit.get("status") != "succeeded":
        return {"status": "exhibit_unavailable",
                "reason": str(exhibit.get("failure_reason") or "附件 99.1 没有取回"),
                "exhibit_url": exhibit.get("canonical_url")}
    manifest = _owner_json(directory / "exhibit-manifest.json")
    if manifest is None:
        return {"status": "exhibit_unavailable", "reason": "附件 99.1 的抓取清单缺失"}
    from .public_web_core_fetch import PublicWebCoreFetchError, validate_public_web_fetch_manifest

    try:
        manifest = validate_public_web_fetch_manifest(manifest)
    except (PublicWebCoreFetchError, ValueError) as exc:
        return {"status": "exhibit_unavailable", "reason": f"附件 99.1 的抓取清单无效：{exc}"}
    if (exhibit.get("manifest_ref") != manifest["id"]
            or exhibit.get("manifest_hash") != manifest["content_hash"]):
        return {"status": "exhibit_unavailable", "reason": "附件 99.1 的抓取记录与清单不一致"}
    accession = document_ref.removeprefix("sec:filing:")
    if f"/{accession.replace('-', '')}/" not in str(manifest["canonical_url"]):
        return {"status": "exhibit_unavailable", "reason": "附件 99.1 不在这份 8-K 自己的目录下，拒绝读取"}
    if int(manifest.get("body_bytes") or 0) > MAX_EXHIBIT_BYTES:
        return {"status": "exhibit_unavailable", "reason": "附件 99.1 超过大小上限，不是一份新闻稿"}
    from .acquired_sec_filings import _spool_bytes

    raw, reason = _spool_bytes(Path(state_dir), manifest["body_sha256"])
    if raw is None:
        return {"status": "exhibit_unavailable", "reason": reason or "附件 99.1 的原始文件不见了"}
    if raw.startswith(b"\x1f\x8b"):
        import gzip

        try:
            raw = gzip.decompress(raw)
        except OSError:
            return {"status": "exhibit_unavailable", "reason": "附件 99.1 的压缩内容损坏"}
    return {"status": "fetched", "manifest": manifest, "bytes": raw,
            "exhibit_url": manifest["canonical_url"], "body_sha256": manifest["body_sha256"]}


def acquired_release_filings(connection: sqlite3.Connection, company_ref: str) -> list[dict[str, Any]]:
    """Every acquired 8-K of the earnings-release spec for one company, newest first."""

    from .mission_company_cik import index_company_refs
    from .sec_plan_specs import EARNINGS_RELEASE_SPEC_REF

    refs = index_company_refs(connection, company_ref)
    try:
        rows = connection.execute(
            "SELECT d.company_ref,d.document_ref,d.ticket_ref,d.discovery_ref,d.updated_at,"
            "s.source_envelope_ref FROM coverage_mission_discovered_documents d "
            "JOIN coverage_mission_source_discoveries s ON s.record_id=d.discovery_ref "
            f"WHERE d.company_ref IN ({','.join('?' * len(refs))}) AND d.source_ref='source:sec-edgar' "
            "AND d.status='acquired' AND s.spec_ref=? ORDER BY d.updated_at DESC",
            (*refs, EARNINGS_RELEASE_SPEC_REF),
        ).fetchall()
    except sqlite3.Error:
        return []
    seen: set[str] = set()
    out = []
    for row in rows:
        if row[1] in seen:
            continue
        seen.add(row[1])
        out.append({"company_ref": company_ref, "document_ref": row[1], "ticket_ref": row[2],
                    "discovery_ref": row[3], "accession": str(row[1]).removeprefix("sec:filing:")})
    return out


# ---------------------------------------------------------------------------
# the ledger
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class PreliminaryActualsAuthority:
    """Append-only reads of earnings releases and the preliminary rows they produced."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    def read_for(self, company_ref: str, accession: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT record_json FROM earnings_release_reads WHERE company_ref=? AND accession=? "
            "ORDER BY CASE status WHEN 'parsed' THEN 0 ELSE 1 END, created_at DESC LIMIT 1",
            (company_ref, accession)).fetchone()
        return None if row is None else json.loads(row[0])

    def record_read(self, read: Mapping[str, Any], rows: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
        """One read and its rows, atomically; a repeat of the same read is a duplicate."""

        if read.get("status") not in READ_STATUSES:
            raise EarningsReleaseError("earnings release read status is outside the vocabulary")
        identity = {key: read.get(key) for key in ("company_ref", "accession", "status", "body_sha256",
                                                   "parser_version")}
        read_id = "earnings-release-read:" + content_hash(identity)[:32]
        record = {"schema_version": SCHEMA_VERSION, "id": read_id, "created_at": _now(),
                  **{key: read.get(key) for key in (
                      "company_ref", "accession", "status", "reason", "period", "period_start",
                      "period_end", "report_date", "items", "exhibit_url", "body_sha256",
                      "document_ref", "ticket_ref", "parser_version", "table")},
                  "row_count": len(rows)}
        record["content_hash"] = content_hash(record)
        with self.store._transaction() as cur:
            if cur.execute("SELECT 1 FROM earnings_release_reads WHERE read_id=?", (read_id,)).fetchone():
                return {"status": "duplicate", "read_id": read_id}
            cur.execute(
                "INSERT INTO earnings_release_reads(read_id,company_ref,accession,status,reason,period,"
                "exhibit_url,body_sha256,row_count,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (read_id, record["company_ref"], record["accession"], record["status"], record["reason"],
                 record["period"], record["exhibit_url"], record["body_sha256"], len(rows),
                 canonical_json(record), record["content_hash"], record["created_at"]))
            for row in rows:
                body = {"schema_version": SCHEMA_VERSION, "read_ref": read_id,
                        "company_ref": record["company_ref"], "accession": record["accession"],
                        "period": record["period"], "period_start": record["period_start"],
                        "period_end": record["period_end"], "report_date": record["report_date"],
                        "status": "preliminary", "basis": PRELIMINARY_BASIS,
                        "label_note": PRELIMINARY_LABEL, "exhibit_url": record["exhibit_url"],
                        "body_sha256": record["body_sha256"],
                        **{key: row[key] for key in ("metric", "label", "label_as_filed", "value", "unit",
                                                     "scale", "span", "excerpt", "excerpt_hash")}}
                row_id = "preliminary-actual:" + content_hash({
                    "company_ref": body["company_ref"], "accession": body["accession"],
                    "metric": body["metric"], "body_sha256": body["body_sha256"]})[:32]
                body["id"] = row_id
                body["created_at"] = record["created_at"]
                body["content_hash"] = content_hash(body)
                cur.execute(
                    "INSERT OR IGNORE INTO preliminary_actuals(row_id,read_ref,company_ref,accession,period,"
                    "metric,value,unit,scale,span_start,span_end,record_json,content_hash,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row_id, read_id, body["company_ref"], body["accession"], body["period"],
                     body["metric"], body["value"], body["unit"], body["scale"], body["span"][0],
                     body["span"][1], canonical_json(body), body["content_hash"], body["created_at"]))
        return {"status": "fresh", "read_id": read_id, "row_count": len(rows)}


PARSER_VERSION = "press-release-income-statement:0.1"


def ingest_company(
    store: Any, *, company_ref: str, state_dir: Path | None = None,
) -> dict[str, Any]:
    """Read every acquired earnings-release exhibit this company has not been read for.

    Idempotent: a release already read with the same bytes and parser is a
    duplicate.  No network and no model call.
    """

    connection = store.connection
    state = state_dir if state_dir is not None else core_state_dir(connection)
    report: dict[str, Any] = {"company_ref": company_ref, "reads": [], "fresh": 0}
    if state is None:
        report["status"] = "unavailable"
        return report
    authority = PreliminaryActualsAuthority(store)
    releases = {row["accession"]: row for row in releases_with_periods(
        connection, company_ref, state_dir=state).get("releases") or ()}
    for filing in acquired_release_filings(connection, company_ref)[:12]:
        release = releases.get(filing["accession"])
        if release is None or EARNINGS_RELEASE_ITEM not in release.get("items", ()):
            continue
        exhibit = exhibit_for_ticket(state, filing["ticket_ref"], filing["document_ref"])
        if exhibit["status"] in ("no_ticket", "not_attempted"):
            report["reads"].append({"accession": filing["accession"], "status": exhibit["status"],
                                    "reason": exhibit["reason"]})
            continue
        read: dict[str, Any] = {
            "company_ref": company_ref, "accession": filing["accession"],
            "period": release.get("period"), "period_start": release.get("period_start"),
            "period_end": release.get("period_end"), "report_date": release.get("report_date"),
            "items": ",".join(release.get("items") or ()), "document_ref": filing["document_ref"],
            "ticket_ref": filing["ticket_ref"], "exhibit_url": exhibit.get("exhibit_url"),
            "body_sha256": exhibit.get("body_sha256"), "parser_version": PARSER_VERSION,
        }
        rows: list[dict[str, Any]] = []
        if exhibit["status"] != "fetched":
            read.update(status="exhibit_unavailable", reason=exhibit["reason"])
        elif not release.get("period"):
            read.update(status="unparseable",
                        reason="公司的财年年末未知，无法确定这份业绩稿对应哪个财季")
        else:
            parsed = parse_press_release(exhibit["bytes"], expected_period_end=release["period_end"])
            if parsed["status"] == "parsed":
                rows = parsed["rows"]
                read.update(status="parsed", reason=None, table=parsed["table"])
            else:
                read.update(status="unparseable", reason=parsed["reason"])
        written = authority.record_read(read, rows)
        report["fresh"] += written["status"] == "fresh"
        report["reads"].append({"accession": filing["accession"], "status": read["status"],
                                "reason": read.get("reason"), "rows": len(rows),
                                "write": written["status"]})
    report["status"] = "read"
    from .model_cell_reconciliation import ModelCellReconciliationAuthority
    compared = ModelCellReconciliationAuthority(store).refresh(company_ref)
    report["model_cell_reconciliation"] = {"cells": len(compared), "compared": sum(row["status"] == "compared" for row in compared), "unavailable": sum(row["status"] == "unavailable" for row in compared)}
    return report


# ---------------------------------------------------------------------------
# reading the ledger: preliminary until filed
# ---------------------------------------------------------------------------


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    try:
        return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                  (name,)).fetchone() is not None
    except sqlite3.Error:
        return False


def _filed_line(connection: sqlite3.Connection, company_ref: str, metric: str,
                filing: Mapping[str, Any], period_end: str | None) -> dict[str, Any] | None:
    concepts = METRIC_CONCEPTS.get(metric)
    if not concepts or not period_end or not filing.get("ingest_id"):
        return None
    try:
        rows = connection.execute(
            "SELECT concept,period_start,period_end,value,unit FROM coverage_mission_statement_lines "
            f"WHERE ingest_id=? AND concept IN ({','.join('?' * len(concepts))}) AND dimension_axis IS NULL "
            "AND period_end=?", (filing["ingest_id"], *concepts, period_end)).fetchall()
    except sqlite3.Error:
        return None
    for row in rows:
        start = row[1]
        if start is None:
            continue
        try:
            days = (date.fromisoformat(row[2]) - date.fromisoformat(start)).days
        except ValueError:
            continue
        if 80 <= days <= 100:
            return {"concept": row[0], "value": row[3], "unit": row[4],
                    "period": f"{row[1]}..{row[2]}"}
    return None


def preliminary_actuals(
    connection: sqlite3.Connection, company_ref: str, *, include_superseded: bool = True,
) -> list[dict[str, Any]]:
    """Every preliminary row for one company, each marked ``preliminary`` or ``superseded``.

    Superseded is decided here, at read time, against the statement-filing
    ledger: once a 10-Q/10-K for the row's fiscal quarter is ingested the row
    is ``superseded`` and carries the filing (and the filed line, when one
    with the same concept and a quarter's duration exists).  Nothing is ever
    updated, so the same ledger answers the same way later.
    """

    if not _table_exists(connection, "preliminary_actuals"):
        return []
    from .mission_company_cik import company_ref_variants

    refs = company_ref_variants(company_ref)
    rows = connection.execute(
        f"SELECT record_json FROM preliminary_actuals WHERE company_ref IN ({','.join('?' * len(refs))}) "
        "ORDER BY created_at DESC, row_id", refs).fetchall()
    if not rows:
        return []
    fye = fiscal_year_end_month(connection, company_ref)
    filed = filed_quarters(connection, company_ref, fye)
    out = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        record = json.loads(row[0])
        key = (str(record.get("period")), str(record.get("metric")))
        if key in seen:
            continue  # the newest read of a quarter wins
        seen.add(key)
        filing = filed.get(str(record.get("period")))
        if filing is not None:
            record["status"] = "superseded"
            record["superseded_by"] = {"accession": filing["accession"], "form": filing["form"],
                                       "report_date": filing["report_date"]}
            line = _filed_line(connection, company_ref, record["metric"], filing,
                               record.get("period_end"))
            if line is not None:
                record["filed_line"] = line
            if not include_superseded:
                continue
        record["source"] = (f"8-K {record['accession']} 附件 99.1，字符 "
                            f"{record['span'][0]}–{record['span'][1]}")
        out.append(record)
    return out


def latest_preliminary(connection: sqlite3.Connection, company_ref: str) -> dict[str, Any] | None:
    """The newest quarter's preliminary rows, for a card: ``{period, rows, status, ...}``."""

    rows = preliminary_actuals(connection, company_ref)
    if not rows:
        return None
    newest = max(rows, key=lambda row: (str(row.get("period_end") or ""), str(row.get("report_date") or "")))
    same = [row for row in rows if row.get("period") == newest.get("period")]
    order = list(METRIC_LABELS)
    same.sort(key=lambda row: order.index(row["metric"]) if row["metric"] in order else 99)
    status = "superseded" if all(row["status"] == "superseded" for row in same) else "preliminary"
    return {"period": newest.get("period"), "period_end": newest.get("period_end"),
            "report_date": newest.get("report_date"), "accession": newest.get("accession"),
            "exhibit_url": newest.get("exhibit_url"), "status": status, "rows": same,
            "label_note": PRELIMINARY_LABEL,
            "provisional_reconciliations": provisional_reconciliations(connection, company_ref, same)}


def failed_reads(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Releases whose newest read failed and that have no parsed read, per company."""

    if not _table_exists(connection, "earnings_release_reads"):
        return []
    rows = connection.execute(
        "SELECT r.record_json FROM earnings_release_reads r WHERE r.status!='parsed' "
        "AND NOT EXISTS (SELECT 1 FROM earnings_release_reads p WHERE p.company_ref=r.company_ref "
        "AND p.accession=r.accession AND p.status='parsed') ORDER BY r.created_at DESC").fetchall()
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        record = json.loads(row[0])
        key = (record["company_ref"], record["accession"])
        if key in seen:
            continue
        seen.add(key)
        out.append(record)
    return out


# ---------------------------------------------------------------------------
# provisional forecast reconciliation
# ---------------------------------------------------------------------------


def provisional_reconciliations(
    connection: sqlite3.Connection, company_ref: str,
    rows: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """The forecast reconciliation, against the preliminary revenue.

    The same arithmetic and bands as ``forecast_reconciliation`` (revenue,
    rescaled into the forecast line's unit; ``deviation_percent``; 1% / 3%),
    computed on read and never written: ``forecast_reconciliations`` holds
    filed actuals only.  When the filed reconciliation for the period exists
    the provisional one says so and points at it -- the re-run on the filed
    number is the existing lane's, unchanged.
    """

    from .forecast_reconciliation import (
        METRIC_BINDINGS, SCALE_FACTORS, _band, _direction, validate_forecast_line,
    )

    if rows is None:
        rows = preliminary_actuals(connection, company_ref)
    revenue = [row for row in rows if row.get("metric") == "revenue" and row.get("unit") == "USD"]
    if not revenue or not _table_exists(connection, "model_forecast_line_versions"):
        return []
    try:
        lines = connection.execute(
            "SELECT v.record_json FROM model_forecast_line_versions v JOIN ("
            "SELECT line_ref, MAX(version_number) AS n FROM model_forecast_line_versions "
            "GROUP BY line_ref) latest ON latest.line_ref=v.line_ref AND latest.n=v.version_number "
            "WHERE v.subject_ref=?", (company_ref,)).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for raw in lines:
        try:
            line = validate_forecast_line(json.loads(raw[0]))
        except Exception:  # noqa: BLE001 - a line this cannot read is not reconciled
            continue
        if line["metric_or_aspect"] not in METRIC_BINDINGS or line.get("currency") != "USD":
            continue
        for row in revenue:
            end = row.get("period_end")
            if not end:
                continue
            try:
                gap = abs((date.fromisoformat(line["period"]["end"]) - date.fromisoformat(end)).days)
                length = (date.fromisoformat(line["period"]["end"])
                          - date.fromisoformat(line["period"]["start"])).days
            except (KeyError, ValueError):
                continue
            if gap > 7 or not 80 <= length <= 100:
                continue
            scale = SCALE_FACTORS.get(line["unit"])
            if scale is None:
                continue
            actual = (Decimal(row["value"]) * SCALES[row["scale"]] / scale).quantize(
                Decimal("0.00000001"), ROUND_HALF_UP)
            forecast = Decimal(str(line["value"]))
            if forecast == 0:
                continue
            deviation = actual - forecast
            percent = (deviation / forecast * 100).quantize(Decimal("0.0001"), ROUND_HALF_UP)
            filed = None
            if _table_exists(connection, "forecast_reconciliations"):
                hit = connection.execute(
                    "SELECT reconciliation_id, band FROM forecast_reconciliations WHERE subject_ref=? "
                    "AND metric_ref=? AND period_end=? ORDER BY created_at DESC LIMIT 1",
                    (company_ref, line["metric_or_aspect"], line["period"]["end"])).fetchone()
                if hit is not None:
                    filed = {"reconciliation_ref": hit[0], "band": hit[1]}
            out.append({
                "provisional": True, "metric_ref": line["metric_or_aspect"],
                "period": f"{line['period']['start']}..{line['period']['end']}",
                "forecast_line_version_ref": line["id"], "forecast_value": format(forecast, "f"),
                # Batch am: which forecast this reconciles against, for the
                # analyst's sentence (「较我们的预测（v3，2026-09-30）偏离 …」).
                "forecast_version": line.get("version"),
                "forecast_created_at": line.get("created_at"),
                "actual_value": format(actual.normalize(), "f"), "unit": line["unit"],
                "deviation_percent": format(percent, "f"), "direction": _direction(deviation),
                "band": _band(percent), "preliminary_row_ref": row["id"],
                "status": "superseded" if (filed or row.get("status") == "superseded") else "provisional",
                "filed_reconciliation": filed,
                "note": "初步对账：用业绩稿的营业收入，申报数到后按申报数重新对账",
            })
    return out


# ---------------------------------------------------------------------------
# events, claims and text
# ---------------------------------------------------------------------------


def amount_text(row: Mapping[str, Any]) -> str:
    """'176.0 亿美元' style text for a row, without changing its digits' meaning."""

    try:
        value = Decimal(str(row["value"])) * SCALES[str(row["scale"])]
    except (InvalidOperation, KeyError):
        return str(row.get("value"))
    unit = row.get("unit")
    if unit == "USD/share":
        return f"{value:.2f} 美元/股"
    if unit == "shares":
        return f"{(value / Decimal(10) ** 6).quantize(Decimal('0.1'))} 百万股"
    if abs(value) >= Decimal(10) ** 8:
        return f"{(value / Decimal(10) ** 8).quantize(Decimal('0.01'))} 亿美元"
    return f"{(value / Decimal(10) ** 4).quantize(Decimal('0.1'))} 万美元"


def preliminary_claim_rows(connection: sqlite3.Connection, company_ref: str) -> list[dict[str, Any]]:
    """The preliminary rows in claim shape, for the quantitative-claim lane's report.

    They are *not* admitted to ``claim_versions``: the Ledger's quantitative
    Claims are filed numbers, and a press-release number reaching it would be
    one a later filed number could tie with.  The shape is the promoter's so a
    reader can put them side by side; ``basis`` and ``tier`` say what they are.
    """

    out = []
    for row in preliminary_actuals(connection, company_ref, include_superseded=False):
        out.append({
            "origin_kind": "preliminary_actual", "origin_ref": row["id"], "company_ref": company_ref,
            "metric_or_aspect": row["metric"], "period": f"{row.get('period_start')}..{row.get('period_end')}",
            "fiscal_period": row.get("period"), "basis": PRELIMINARY_BASIS, "tier": "preliminary",
            "value": row["value"], "unit": row["unit"], "scale": row["scale"],
            "source_document_ref": f"sec:filing:{row['accession']}",
            "normalized_statement": (
                f"{row.get('period')}（截至 {row.get('period_end')}）{row['label']}为 {amount_text(row)}"
                f"（{PRELIMINARY_LABEL}；来源 8-K {row['accession']} 附件 99.1，"
                f"字符 {row['span'][0]}–{row['span'][1]}）。"),
            "label_note": PRELIMINARY_LABEL,
        })
    return out


EVENT_KIND = "earnings_release"
_EVENT_HEADLINE = ("revenue", "operating_income", "net_income", "eps_diluted")


def earnings_release_event_candidates(
    connection: sqlite3.Connection, *, company_ref: str,
) -> list[dict[str, Any]]:
    """One ``earnings_release`` event per parsed release whose quarter is not yet filed."""

    if not _table_exists(connection, "earnings_release_reads"):
        return []
    rows = preliminary_actuals(connection, company_ref, include_superseded=False)
    by_accession: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_accession.setdefault(row["accession"], []).append(row)
    out = []
    for accession, group in by_accession.items():
        first = group[0]
        figures = {row["metric"]: row for row in group}
        recon = [item for item in provisional_reconciliations(connection, company_ref, group)
                 if item["status"] == "provisional"]
        payload: dict[str, Any] = {
            "accession": accession, "form": "8-K", "items": EARNINGS_RELEASE_ITEM,
            "report_date": first.get("report_date"), "period": first.get("period"),
            "period_end": first.get("period_end"), "exhibit": EXHIBIT_NUMBER,
            "exhibit_url": first.get("exhibit_url"), "artifact_hash": first.get("body_sha256"),
            "document_ref": f"sec:filing:{accession}", "status": "preliminary",
            "label_note": PRELIMINARY_LABEL, "row_count": len(group),
            "event_key": f"earnings-release:{accession}:{first.get('body_sha256', '')[:16]}",
        }
        for metric in _EVENT_HEADLINE:
            row = figures.get(metric)
            payload[metric] = None if row is None else row["value"]
            payload[f"{metric}_unit"] = None if row is None else f"{row['unit']}:{row['scale']}"
        payload["revenue_vs_forecast_percent"] = recon[0]["deviation_percent"] if recon else None
        payload["revenue_vs_forecast_band"] = recon[0]["band"] if recon else None
        occurred = first.get("report_date") or str(first.get("created_at"))[:10]
        out.append({
            "kind": EVENT_KIND, "company_ref": company_ref,
            "occurred_at": f"{occurred}T00:00:00+00:00",
            "source_refs": [f"sec:filing:{accession}", *(row["id"] for row in group[:8])],
            "payload": payload, "evidence_tier": "management_direct",
        })
    return out


def judgement_lines(event: Mapping[str, Any], connection: sqlite3.Connection) -> list[str]:
    """The derived block the event judge reads for an ``earnings_release`` event."""

    payload = event.get("payload") or {}
    rows = [row for row in preliminary_actuals(connection, str(event.get("company_ref")))
            if row.get("accession") == payload.get("accession")]
    lines = [
        f"Company earnings release (8-K Item 2.02, Exhibit 99.1) for {payload.get('period')} "
        f"ending {payload.get('period_end')}, furnished {payload.get('report_date')}.",
        "These figures are PRELIMINARY (" + PRELIMINARY_LABEL + "): unaudited press-release "
        "numbers that the 10-Q/10-K will replace; never treat them as filed.",
    ]
    for row in rows:
        state = "superseded by " + row["superseded_by"]["form"] if row["status"] == "superseded" else "preliminary"
        lines.append(f"- {row['metric']} ({row['label_as_filed']}): {row['value']} {row['unit']} "
                     f"scale={row['scale']} [{state}; span {row['span'][0]}-{row['span'][1]}]")
    for item in provisional_reconciliations(connection, str(event.get("company_ref")), rows):
        lines.append(f"Provisional revenue vs our forecast: {item['deviation_percent']}% "
                     f"({item['band']}, {item['status']}).")
    return lines


def brief_entries(connection: sqlite3.Connection, company_refs: Sequence[str],
                  *, since: str | None = None) -> list[dict[str, Any]]:
    """The weekly brief's 「最新业绩（初步）」 rows: one per company with a parsed release."""

    out = []
    for company_ref in company_refs:
        latest = latest_preliminary(connection, company_ref)
        if latest is None:
            continue
        if since is not None and str(latest.get("report_date") or "") < since[:10]:
            continue
        out.append({
            "company_ref": company_ref, "period": latest["period"], "status": latest["status"],
            "accession": latest["accession"], "report_date": latest["report_date"],
            "rows": [{"metric": row["metric"], "label": row["label"], "value": row["value"],
                      "unit": row["unit"], "scale": row["scale"], "text": amount_text(row),
                      "status": row["status"], "ref": row["id"], "span": row["span"]}
                     for row in latest["rows"]],
            "provisional_reconciliations": latest["provisional_reconciliations"],
            "note": PRELIMINARY_LABEL,
        })
    return out


def replay_brief_entries(connection: sqlite3.Connection, entries: Sequence[Mapping[str, Any]]) -> None:
    """Every row a published brief cited still exists with the value it showed."""

    if not entries:
        return
    if not _table_exists(connection, "preliminary_actuals"):
        raise EarningsReleaseError("weekly brief cites preliminary actuals this Core does not hold")
    for entry in entries:
        for row in entry.get("rows") or ():
            held = connection.execute(
                "SELECT value, unit, scale FROM preliminary_actuals WHERE row_id=?",
                (row.get("ref"),)).fetchone()
            if held is None or (held[0], held[1], held[2]) != (row.get("value"), row.get("unit"),
                                                             row.get("scale")):
                raise EarningsReleaseError(f"preliminary actual {row.get('ref')} no longer replays")


def brief_lines(entries: Sequence[Mapping[str, Any]], tickers: Mapping[str, str]) -> list[str]:
    if not entries:
        return ["本期没有新的公司业绩稿。"]
    lines: list[str] = []
    for entry in entries:
        name = tickers.get(entry["company_ref"]) or entry["company_ref"]
        state = ("已被 10-Q/10-K 申报数取代，仅作对照" if entry["status"] == "superseded"
                 else PRELIMINARY_LABEL)
        lines.append(f"- {name} {entry['period']}（8-K {entry['accession']}，{entry['report_date']}；{state}）：")
        for row in entry.get("rows") or ():
            lines.append(f"  - {row['label']} {row['text']}｜{row['ref']}")
        for item in entry.get("provisional_reconciliations") or ():
            lines.append(f"  - 初步对账：营业收入较预测偏离 {item['deviation_percent']}%"
                         f"（{item['band']}；{'已按申报数重新对账' if item['status'] == 'superseded' else '申报数到后重新对账'}）")
    return lines


__all__ = [
    "EARNINGS_RELEASE_ITEM",
    "EVENT_KIND",
    "EXHIBIT_NUMBER",
    "LATE_CALIBRATION_MAX_DAYS",
    "METRIC_LABELS",
    "PARSER_VERSION",
    "PRELIMINARY_BASIS",
    "PRELIMINARY_LABEL",
    "PreliminaryActualsAuthority",
    "acquired_release_filings",
    "amount_text",
    "brief_entries",
    "brief_lines",
    "ineligible_filing_refs",
    "replay_brief_entries",
    "core_state_dir",
    "earnings_release_event_candidates",
    "exhibit_for_ticket",
    "exhibit_url",
    "failed_reads",
    "filed_quarters",
    "fiscal_year_end_month",
    "ingest_company",
    "judgement_lines",
    "latest_preliminary",
    "parse_press_release",
    "preliminary_actuals",
    "preliminary_claim_rows",
    "press_release_exhibit_name",
    "exhibit_name_from_filing_index",
    "exhibit_redetect_candidates",
    "exhibit_redetect_due",
    "filing_index_name",
    "EXHIBIT_DETECTOR_VERSION",
    "EXHIBIT_NOT_FOUND_REASON",
    "provisional_reconciliations",
    "release_fiscal_quarter",
    "releases_with_periods",
    "reported_fiscal_quarters",
    "reported_release",
]
