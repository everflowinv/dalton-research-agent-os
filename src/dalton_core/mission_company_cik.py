"""The SEC CIK of a covered company, whatever shape its ref has.

The legacy Core names every company ``company:sec-cik:<cik>``, and several
lanes read the CIK straight out of the ref.  A workspace created through the
setup flow names them ``company:ticker:<ticker>`` (``workspace_mission_setup``)
and resolves the CIK once, at first publish, into its SEC discovery plan.  A
lane that only read the ref skipped every company of such a workspace -- ws-7d's
ownership lane reported "every ownership filing in the window has been read"
over an empty universe, and its catalyst lane never asked SEC for a date.

One reader, in order of authority:

1. the CIK inside a ``company:sec-cik:`` ref;
2. the CIKs SEC itself stamped on the statement filings the mission ingested;
3. the SEC discovery plan the workspace setup resolved from the ticker.

Exactly one distinct CIK or nothing: an ambiguous company is not guessed.
Read-only; never contacts SEC.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path

COMPANY_REF_CIK_PREFIX = "company:sec-cik:"


def cik_in_ref(company_ref: object) -> str | None:
    """The CIK a ``company:sec-cik:`` ref spells, as it spells it."""

    if not isinstance(company_ref, str) or not company_ref.startswith(COMPANY_REF_CIK_PREFIX):
        return None
    cik = company_ref[len(COMPANY_REF_CIK_PREFIX):].strip()
    return cik if cik.isdigit() else None


def resolved_cik(state_dir: str | Path, company_ref: str) -> str | None:
    """The CIK this state directory has already resolved for ``company_ref``."""

    ciks: set[str] = set()
    core = Path(state_dir) / "core.sqlite"
    if core.is_file():
        try:
            from .readonly_sqlite import connect_read_only

            connection = connect_read_only(core)
            try:
                rows = connection.execute(
                    "SELECT DISTINCT cik FROM coverage_mission_statement_filings WHERE company_ref=?",
                    (company_ref,),
                ).fetchall()
            finally:
                connection.close()
            ciks = {str(row[0]).zfill(10) for row in rows
                    if row[0] is not None and str(row[0]).isdigit()}
        except sqlite3.Error:
            ciks = set()
    if not ciks:
        plans = Path(state_dir) / "discovery-plans"
        if plans.is_dir():
            from .mission_source_discovery import SEC_SOURCE_REF, load_discovery_plan

            for path in sorted(plans.glob("*.json")):
                try:
                    plan = load_discovery_plan(path)
                except Exception:  # noqa: BLE001 - another lane's plan shape
                    continue
                if plan.get("source_ref") != SEC_SOURCE_REF:
                    continue
                entry = (plan.get("companies") or {}).get(company_ref)
                if isinstance(entry, Mapping) and str(entry.get("cik", "")).isdigit():
                    ciks.add(str(entry["cik"]).zfill(10))
    return next(iter(ciks)) if len(ciks) == 1 else None


def company_cik(company_ref: object, *, state_dir: str | Path | None = None) -> str | None:
    """The ref's own CIK, else the one this workspace resolved, else None."""

    cik = cik_in_ref(company_ref)
    if cik is not None:
        return cik
    if state_dir is None or not isinstance(company_ref, str) or not company_ref.strip():
        return None
    return resolved_cik(state_dir, company_ref.strip())


def index_company_refs(connection: sqlite3.Connection, company_ref: str) -> list[str]:
    """Every ref the document index may hold ``company_ref``'s filings under.

    ``document_index`` names an SEC ``list_filings`` document by the issuer in
    the call -- ``company:sec-cik:<cik>`` -- whatever the mission calls the
    company.  A ``company:ticker:`` mission asking the index by its own ref
    found none of its filings (buyback and insider-plan reads came back
    empty), so the resolved CIK's ref is asked too.
    """

    refs = [company_ref]
    if cik_in_ref(company_ref) is not None:
        return refs
    try:
        row = connection.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error:
        row = None
    path = row[2] if row is not None and len(row) > 2 else None
    if not path:
        return refs
    cik = resolved_cik(Path(path).parent, company_ref)
    if cik is not None:
        refs.append(f"{COMPANY_REF_CIK_PREFIX}{cik}")
    return refs


__all__ = ["COMPANY_REF_CIK_PREFIX", "cik_in_ref", "company_cik", "index_company_refs",
           "resolved_cik"]
