"""C1: find the company's own earnings announcements in filings already held.

A vendor's earnings date is a forecast about a company's behaviour. The
company's own Item 2.02 8-K -- "Results of Operations and Financial Condition"
-- is the thing itself, and it is the only source in this system that can
confirm a date rather than estimate one.

**No new network call, and no new operation.** The governed ``list_filings``
call the SEC discovery lane already makes asks for one form and gets back the
issuer's whole ``filings.recent`` block, every form in it, and the raw body is
spooled and hashed. Every Item 2.02 8-K these five companies have filed in the
past year is therefore already on disk, unparsed, alongside the ``items``
column that names the item numbers and the ``reportDate`` that dates the event.
This module reads that artifact. It never fetches.

**What that reading may and may not be used for.** The 8-K row is present in
the bytes but is *not* in the invocation's ``source_record_refs``, because that
call asked for 10-Ks. So it may not be turned into a filing URL and fetched --
``sec_filings_index.build_sec_filing_url_authorities`` refuses exactly this,
correctly. It may be cited as a *date*: the accession, the item numbers and the
report date, carried back to one exact governed call by its artifact hash. A
date and a document are different asks and only the first one is made here.

**What this cannot do yet, said plainly.** Many companies file an 8-K some
weeks ahead saying "we will report fourth-quarter results on the 25th". Reading
that sentence needs the filing's *text*, and Dalton has no authority to fetch an
8-K document: that would need a new spec in the discovery plan with
``form: 8-K`` and a fetch, which is a plan version and not this slice. So the
mechanism for a company-confirmed *future* date exists here --
:func:`announced_next_date_entry` -- and takes the date and the accession from
its caller. It parses nothing and it guesses nothing. Until a text reader
exists, a forthcoming earnings date in this system is ``estimated``, and the
calendar says so rather than implying otherwise.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

SOURCE_REF = "source:sec-edgar"
FILINGS_INDEX_OPERATION = "list_filings"
# "Results of Operations and Financial Condition": the item a company files
# when it announces a quarter. Everything else an 8-K can carry is a different
# kind of news and none of it dates an earnings announcement.
EARNINGS_RELEASE_ITEM = "2.02"
FORM = "8-K"
# How far back to look. Matched to the calendar's own retention, so the
# detector does not spend a publish proposing entries the calendar would
# immediately age out.
DEFAULT_LOOKBACK_DAYS = 120
MAX_RELEASES = 40


class SecEarningsReleaseError(RuntimeError):
    """A submissions artifact is not the shape this reader can describe."""


def _column(recent: Mapping[str, Any], name: str, length: int | None = None) -> list[Any]:
    values = recent.get(name)
    if not isinstance(values, list):
        raise SecEarningsReleaseError(f"submissions index has no {name} column")
    if length is not None and len(values) != length:
        raise SecEarningsReleaseError(f"submissions column {name} is a different length")
    return values


def _optional_column(recent: Mapping[str, Any], name: str, length: int) -> list[Any]:
    """A column SEC serves but the frozen normaliser never read.

    Absent rather than fatal: an artifact captured by something that trimmed
    the response is still a usable index of accessions and dates, it just
    cannot say which items an 8-K carried -- and a reader that crashed on that
    would take the whole lane down over a column it can do without.
    """

    values = recent.get(name)
    if not isinstance(values, list) or len(values) != length:
        return [None] * length
    return values


def earnings_release_filings(
    payload: Mapping[str, Any],
    *,
    item: str = EARNINGS_RELEASE_ITEM,
    since: str | None = None,
    limit: int = MAX_RELEASES,
) -> list[dict[str, Any]]:
    """Every Item 2.02 8-K in one submissions artifact, newest first.

    ``items`` is a comma-separated list -- Accenture's June 2026 release is
    ``"2.02,9.01"`` -- so membership is checked against the split parts. A
    substring test would match ``12.02`` if the SEC ever numbered one, and
    silently mis-file it.

    The date the entry takes is ``reportDate``: the day the results were
    announced, which is what a calendar is about. ``filingDate`` is when the
    paperwork arrived and is usually the same day, but not always, and the
    fallback is explicit rather than assumed.
    """

    filings = payload.get("filings")
    if not isinstance(filings, Mapping):
        raise SecEarningsReleaseError("payload is not a SEC submissions index")
    recent = filings.get("recent")
    if not isinstance(recent, Mapping):
        raise SecEarningsReleaseError("submissions index has no recent block")
    accessions = _column(recent, "accessionNumber")
    length = len(accessions)
    forms = _column(recent, "form", length)
    filed = _column(recent, "filingDate", length)
    items = _optional_column(recent, "items", length)
    reported = _optional_column(recent, "reportDate", length)
    accepted = _optional_column(recent, "acceptanceDateTime", length)

    found: list[dict[str, Any]] = []
    for index in range(length):
        if forms[index] != FORM:
            continue
        raw_items = items[index]
        if not isinstance(raw_items, str):
            continue
        parts = [part.strip() for part in raw_items.split(",")]
        if item not in parts:
            continue
        filing_date = filed[index]
        if not isinstance(filing_date, str) or len(filing_date) != 10:
            continue
        report_date = reported[index]
        if not isinstance(report_date, str) or len(report_date) != 10:
            report_date = filing_date
        if since is not None and report_date < since:
            continue
        accession = accessions[index]
        if not isinstance(accession, str) or not accession:
            continue
        acceptance = accepted[index]
        found.append({
            "accession": accession,
            "form": FORM,
            "items": parts,
            "filing_date": filing_date,
            "report_date": report_date,
            "acceptance_datetime": (
                acceptance if isinstance(acceptance, str) and acceptance else None
            ),
        })
    found.sort(key=lambda row: (row["report_date"], row["accession"]), reverse=True)
    return found[: max(0, int(limit))]


def _observed_at(release: Mapping[str, Any]) -> str:
    """When the company said it, to the second where SEC recorded that.

    ``acceptanceDateTime`` is Eastern-stamped without an offset in SEC's own
    feed shape, so it is not trusted as a wall clock; the filing date at
    midnight UTC is used unless the acceptance stamp already carries a zone.
    What this value is *for* is ordering two statements from the same source,
    and the filing date orders them correctly.
    """

    acceptance = release.get("acceptance_datetime")
    if isinstance(acceptance, str) and acceptance.endswith("Z"):
        return acceptance.replace("Z", "+00:00")
    return f"{release['filing_date']}T00:00:00+00:00"


def calendar_entries(
    releases: Sequence[Mapping[str, Any]],
    *,
    invocation_ref: str,
    artifact_hash: str,
    subject_for: Any,
) -> list[dict[str, Any]]:
    """Turn Item 2.02 filings into confirmed ``earnings`` calendar entries.

    These are dates in the *past*: the company has already reported. That is
    worth recording for two reasons. It is the T+0..T+2 calibration window's
    trigger, and it is what turns the vendor's estimate for that quarter from
    ``estimated`` into ``confirmed`` -- the estimate does not disappear, it sits
    beside the confirmed date and the entry says whether the two agree.

    ``subject_for`` is passed in rather than imported so that both sources use
    literally the same occurrence key; two mappers with their own copy of that
    rule is how two sources stop merging.
    """

    entries: list[dict[str, Any]] = []
    for release in releases:
        entries.append({
            "event_kind": "earnings",
            "subject": subject_for(release["report_date"]),
            "sources": [{
                "kind": "filing",
                # Named by accession, so the citation is the document and not
                # the call that happened to list it.
                "ref": f"sec:filing:{release['accession']}",
                "source_ref": SOURCE_REF,
                "observed_date": release["report_date"],
                "observed_at": _observed_at(release),
                "confidence": "confirmed",
                "note": (
                    f"8-K Item {EARNINGS_RELEASE_ITEM} "
                    f"({','.join(release['items'])}) via {invocation_ref} "
                    f"artifact {artifact_hash[:12]}"
                ),
            }],
            "notes": "",
        })
    return entries


def announced_next_date_entry(
    *,
    accession: str,
    announced_date: str,
    subject: str,
    filing_date: str,
    note: str = "",
    event_kind: str = "earnings",
) -> dict[str, Any]:
    """A future date the company itself announced, taken from a caller.

    The seam for the "we will report on the 25th" 8-K. It exists so that the
    confirmed-future path is built, tested and reachable rather than being
    designed later against whatever the text reader happens to produce -- and
    it takes the date as an argument because nothing in this module reads a
    filing's text, and a function that guessed one would be the single worst
    thing this file could contain.
    """

    day = str(announced_date)
    date.fromisoformat(day)
    date.fromisoformat(str(filing_date))
    return {
        "event_kind": event_kind,
        "subject": subject,
        "sources": [{
            "kind": "filing",
            "ref": f"sec:filing:{accession}",
            "source_ref": SOURCE_REF,
            "observed_date": day,
            "observed_at": f"{filing_date}T00:00:00+00:00",
            "confidence": "confirmed",
            "note": note or f"date announced in {accession}",
        }],
        "notes": "",
    }


# -- reading what is already held ------------------------------------------


def submissions_artifacts(
    connection: Any, *, issuer: str | None = None
) -> list[dict[str, Any]]:
    """Every governed ``list_filings`` call whose raw body is still spooled.

    The join the rest of this codebase already uses to answer "what did a
    connector call return": envelope to invocation to call spec. Newest first,
    because a company's index is only as fresh as its last discovery run and
    the newest one is the only one worth reading.
    """

    try:
        rows = connection.execute(
            "SELECT e.source_envelope_id AS envelope_ref, "
            "e.raw_response_hash AS hash, "
            "e.connector_invocation_ref AS invocation_ref, c.record_json AS spec, "
            "e.rowid AS position FROM connector_source_envelopes e "
            "JOIN connector_invocations i "
            "ON i.connector_invocation_id=e.connector_invocation_ref "
            "JOIN connector_call_specs c ON c.call_spec_id=i.call_spec_ref "
            "WHERE c.operation=? AND e.status IN ('complete','partial') "
            "ORDER BY e.rowid DESC",
            (FILINGS_INDEX_OPERATION,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        # A Core that has never made a connector call has no connector tables.
        # That is "nothing has been read yet", which is a state this lane
        # reports rather than fails on; anything else about the query is a real
        # problem and is not swallowed.
        if "no such table" not in str(exc):
            raise
        return []
    found: list[dict[str, Any]] = []
    for row in rows:
        try:
            spec = json.loads(row["spec"])
        except (TypeError, ValueError):
            continue
        parameters = spec.get("parameters")
        if not isinstance(parameters, Mapping):
            continue
        row_issuer = str(parameters.get("issuer") or "")
        if issuer is not None and row_issuer.lstrip("0") != str(issuer).lstrip("0"):
            continue
        found.append({
            "envelope_ref": row["envelope_ref"],
            "invocation_ref": row["invocation_ref"],
            "artifact_hash": row["hash"],
            "issuer": row_issuer,
            "parameters": dict(parameters),
        })
    return found


def releases_for_issuer(
    connection: Any,
    state_dir: str | Path,
    *,
    issuer: str,
    today: str,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict[str, Any]:
    """The company's own recent earnings announcements, from spooled bytes.

    Returns the releases and the exact call they were read out of, or a reason
    nothing could be read. A reason rather than an exception: a company whose
    discovery run has not happened yet is a normal state of this system, and
    the lane wants to say so in its tick summary rather than fail.
    """

    from .mission_sec_quarters import read_artifact  # noqa: PLC0415 - lazy

    since = (
        date.fromisoformat(today) - timedelta(days=max(0, int(lookback_days)))
    ).isoformat()
    root = Path(state_dir)
    for artifact in submissions_artifacts(connection, issuer=issuer):
        payload = read_artifact(root, artifact["artifact_hash"])
        if payload is None:
            continue
        try:
            releases = earnings_release_filings(payload, since=since)
        except SecEarningsReleaseError as exc:
            return {"status": "unreadable", "reason": str(exc), **artifact}
        return {
            "status": "read", "releases": releases, "since": since, **artifact,
        }
    return {
        "status": "unavailable",
        "reason": (
            f"no spooled SEC filings index for issuer {issuer}; the discovery "
            "lane has not run for this company or its artifact has been pruned"
        ),
        "releases": [],
        "since": since,
    }


__all__ = [
    "DEFAULT_LOOKBACK_DAYS",
    "EARNINGS_RELEASE_ITEM",
    "FILINGS_INDEX_OPERATION",
    "FORM",
    "MAX_RELEASES",
    "SOURCE_REF",
    "SecEarningsReleaseError",
    "announced_next_date_entry",
    "calendar_entries",
    "earnings_release_filings",
    "releases_for_issuer",
    "submissions_artifacts",
]
