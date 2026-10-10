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

import re
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

COMPANY_REF_CIK_PREFIX = "company:sec-cik:"


def cik_in_ref(company_ref: object) -> str | None:
    """The CIK a ``company:sec-cik:`` ref spells, as it spells it."""

    if not isinstance(company_ref, str) or not company_ref.startswith(COMPANY_REF_CIK_PREFIX):
        return None
    cik = company_ref[len(COMPANY_REF_CIK_PREFIX):].strip()
    return cik if cik.isdigit() else None


#: SEC CIKs are ten digits.  A ``company:sec-cik:`` ref is minted with the
#: CIK zero-padded to ten (:func:`sec_cik_company_ref`).
SEC_CIK_DIGITS = 10


def sec_cik_company_ref(cik: object) -> str:
    """The canonical ``company:sec-cik:`` ref for ``cik`` (ten digits).

    Batch u (2026-10-07): DXC was minted ``company:sec-cik:001688568`` --
    nine digits -- in the packaged SEC catalog (p8a-s7f) and that string is
    now the legacy mission's company identity in every append-only table.
    It is not rewritten; every *new* ref is minted here, and lookups that
    may meet either spelling use :func:`same_company_ref` /
    :func:`company_ref_variants`.
    """

    text = str(cik).strip()
    text = text[len(COMPANY_REF_CIK_PREFIX):] if text.startswith(COMPANY_REF_CIK_PREFIX) else text
    text = text.removeprefix("CIK")
    if not text.isdigit() or len(text.lstrip("0") or "0") > SEC_CIK_DIGITS:
        raise ValueError(f"not an SEC CIK: {cik!r}")
    return f"{COMPANY_REF_CIK_PREFIX}{int(text):0{SEC_CIK_DIGITS}d}"


def canonical_company_ref(company_ref: str) -> str:
    """``company_ref`` with a ``company:sec-cik:`` CIK padded to ten digits."""

    cik = cik_in_ref(company_ref)
    return company_ref if cik is None else sec_cik_company_ref(cik)


def is_canonical_company_ref(company_ref: object) -> bool:
    """False only for a ``company:sec-cik:`` ref whose CIK is not ten digits."""

    cik = cik_in_ref(company_ref)
    return cik is None or len(cik) == SEC_CIK_DIGITS


def same_company_ref(left: object, right: object) -> bool:
    """Whether two refs name the same company (CIK padding aside)."""

    if not isinstance(left, str) or not isinstance(right, str):
        return False
    if left == right:
        return True
    a, b = cik_in_ref(left), cik_in_ref(right)
    return a is not None and b is not None and int(a) == int(b)


def company_ref_variants(company_ref: str) -> list[str]:
    """Every spelling a store may hold ``company_ref`` under, exact one first."""

    refs = [company_ref]
    cik = cik_in_ref(company_ref)
    if cik is not None:
        for spelling in (sec_cik_company_ref(cik),
                         *(ref for ref in LEGACY_NONCANONICAL_COMPANY_REFS
                           if same_company_ref(ref, company_ref))):
            if spelling not in refs:
                refs.append(spelling)
    return refs


def resolve_company_ref(company_ref: str, known: Any) -> str:
    """The spelling in ``known`` that names ``company_ref``'s company, else it."""

    known = list(known or ())
    if company_ref in known:
        return company_ref
    return next((ref for ref in known if same_company_ref(ref, company_ref)), company_ref)


#: Non-canonical refs already in append-only history.  Kept as they are;
#: parity reports them (``mission.universe.company_ref_canonical``).
LEGACY_NONCANONICAL_COMPANY_REFS = ("company:sec-cik:001688568",)


class NonCanonicalCompanyRef(ValueError):
    """A ``company:sec-cik:`` ref whose CIK is not zero-padded to ten digits."""


def validate_company_ref(company_ref: object) -> str:
    """``company_ref`` unchanged when canonical; refuse a short-CIK spelling.

    Batch an (2026-10-09): every ref *built* from now on carries a ten-digit
    CIK.  A nine-digit ``company:sec-cik:001688568`` is refused here rather
    than padded silently, so the caller that minted it is found; history that
    already holds such a spelling is read through :func:`resolve_alias` /
    :func:`same_company_ref` and is never rewritten.
    """

    if not isinstance(company_ref, str) or not company_ref.strip():
        raise NonCanonicalCompanyRef("company_ref must be a non-empty string")
    cik = cik_in_ref(company_ref)
    if company_ref.startswith(COMPANY_REF_CIK_PREFIX) and cik is None:
        raise NonCanonicalCompanyRef(f"not an SEC CIK ref: {company_ref!r}")
    if cik is not None and len(cik) != SEC_CIK_DIGITS:
        raise NonCanonicalCompanyRef(
            f"{company_ref!r} carries a {len(cik)}-digit CIK; company refs are minted "
            f"with ten digits ({sec_cik_company_ref(cik)})")
    return company_ref


#: Every spelling of a SEC-CIK company ref that appears in stored text: the
#: ref itself and the dashed slug some version ids embed
#: (``company-dossier-version:company-sec-cik-0000051143:31``).
_CIK_REF_RE = re.compile(r"company(?P<sep>[:-])sec-cik(?P=sep)(?P<cik>\d+)")


def noncanonical_refs_in_text(text: object) -> list[str]:
    """The short-CIK company refs (either spelling) that ``text`` contains."""

    if not isinstance(text, str) or "sec-cik" not in text:
        return []
    out = []
    for match in _CIK_REF_RE.finditer(text):
        if len(match.group("cik")) != SEC_CIK_DIGITS and match.group(0) not in out:
            out.append(match.group(0))
    return out


def canonical_spelling(ref: str) -> str:
    """The ten-digit form of either spelling (``:`` ref or ``-`` slug)."""

    match = _CIK_REF_RE.fullmatch(ref)
    if match is None:
        return ref
    sep = match.group("sep")
    return f"company{sep}sec-cik{sep}{int(match.group('cik')):0{SEC_CIK_DIGITS}d}"


def resolve_alias(company_ref: str, aliases: Mapping[str, str] | None = None) -> str:
    """The canonical ref ``company_ref`` names: an alias row, else CIK padding.

    ``aliases`` is ``product_consistency.company_ref_aliases`` (alias ->
    canonical); without it the CIK is padded, which is the same answer for a
    ``company:sec-cik:`` ref.  A ``company:ticker:`` ref is its own canonical.
    """

    if aliases and company_ref in aliases:
        return aliases[company_ref]
    return canonical_company_ref(company_ref)


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
        # The index mints ten-digit refs; a legacy nine-digit mission ref
        # (DXC) must still find them.
        return company_ref_variants(company_ref)
    try:
        row = connection.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error:
        row = None
    path = row[2] if row is not None and len(row) > 2 else None
    if not path:
        return refs
    cik = resolved_cik(Path(path).parent, company_ref)
    if cik is not None:
        refs.append(sec_cik_company_ref(cik))
    return refs


__all__ = ["COMPANY_REF_CIK_PREFIX", "LEGACY_NONCANONICAL_COMPANY_REFS", "NonCanonicalCompanyRef",
           "SEC_CIK_DIGITS", "canonical_company_ref", "canonical_spelling", "noncanonical_refs_in_text",
           "resolve_alias", "validate_company_ref", "cik_in_ref", "company_cik", "company_ref_variants",
           "index_company_refs", "is_canonical_company_ref", "resolve_company_ref",
           "resolved_cik", "same_company_ref", "sec_cik_company_ref"]
