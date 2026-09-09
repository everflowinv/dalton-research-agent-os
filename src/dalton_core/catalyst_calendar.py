"""C1: when each covered company will next speak, and who says so.

An analyst's year is organised around dates: the quarter closes, the company
reports, the model is either right or it is not, and the four weeks before the
report are when the work that decides that gets done. Dalton could read what a
company had already filed and had no idea when the next one was due, so the
earnings-season workflow (blueprint P14f: a preview about a month out, a
calibration inside forty-eight hours afterwards) had nothing to fire on.

This is the calendar those windows are computed from. It is deliberately a
*record of who said what*, not a schedule:

**Two sources, never averaged.** Yahoo's calendar gives a date without saying
where it came from, so a date from there is ``estimated`` and can never be
anything else on its own. A company announcing its own date -- the 8-K under
Item 2.02 that says "we will report fourth-quarter results on the 25th" -- is
``confirmed``, because the only body that can confirm when a company will speak
is the company. When the two disagree, both dates stay in the entry with the
confidence each deserves and the entry says it is in disagreement. Nothing is
split, averaged or reconciled: an earnings call does not happen on the mean of
two guesses.

**A confirmed date wins, and that is a rule rather than a judgement.**
``SOURCE_AUTHORITY`` is a frozen ordering, so which date an entry asserts is
derivable from the entry rather than decided somewhere else, and the losing
date is still there to read.

**Never a guessed date.** Every entry names the invocation or the accession
that produced its date. There is no inference step: a quarter end plus the
usual four weeks is not a catalyst, it is arithmetic dressed as a fact, and
this module will not produce one.

**Append-only, under ADR-0008.** A version is published only when an entry is
added, a date moves, or a confidence changes; anything else is a ``duplicate``
that costs a read. The version carries the ``change_reason`` those changes
require -- ``driver_event`` for a date that moved, ``evidence_thicker`` for an
estimate that became confirmed -- and refuses a caller whose stated reason is
not the one the diff shows, so the chain reads back as a history rather than
as a log of writes.

One thing ADR-0008 asks for is done by the chain rather than inside the entry.
A superseded *reading* -- yesterday's Yahoo date, replaced by today's -- is not
kept beside the new one, because a source that is re-read every morning would
otherwise grow an entry without bound. It is kept in the previous version,
which is never deleted, and the change is named in that version's ``changes``
with its ``from`` and ``to``. What was superseded is therefore always
recoverable; it just lives one link back rather than in place.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .store import canonical_json, content_hash

SCHEMA_VERSION = "0.1"
ACTOR_REF = "core:catalyst-calendar-worker"

# What kind of thing is on the calendar.
EVENT_KINDS: tuple[str, ...] = (
    # A results announcement: the date the P14f windows are computed from.
    "earnings",
    # A guidance update outside a results announcement -- a pre-announcement,
    # a raise, a withdrawal.
    "guidance",
    # An analyst or investor day.
    "investor_day",
    # A statutory deadline: a 10-K or 10-Q due date.
    "filing_due",
    # The day the shares trade without the declared dividend.
    "ex_dividend",
    "other",
)
CONFIDENCES: tuple[str, ...] = ("confirmed", "estimated")

# Where a date came from.
SOURCE_KINDS: tuple[str, ...] = (
    # One governed connector call: a market-data vendor's opinion about a date.
    "connector_invocation",
    # A document the company itself filed, named by accession.
    "filing",
)
# The only kind of source that can confirm a date. A vendor saying a company
# will report on the 24th is a forecast about a company's behaviour, however
# confidently it is presented; the company saying it in an 8-K is the fact.
CONFIRMING_SOURCE_KINDS: frozenset[str] = frozenset({"filing"})
# Which source's date the entry asserts when sources disagree. Frozen and
# named, so "the confirmed one wins" is a property of the record rather than
# something a caller decided once and nobody can see.
SOURCE_AUTHORITY: Mapping[str, int] = {"filing": 2, "connector_invocation": 1}

# ADR-0008's closed vocabulary. Declared here rather than imported so that this
# module stays cheap to import from a lane; a test pins it to the one other
# authority that carries it, so the two cannot drift apart in silence.
CHANGE_REASONS: tuple[str, ...] = (
    "filing_actual", "driver_event", "assumption_review", "evidence_thicker",
    "human_revision",
)
# What a version's ``changes`` can say happened to one entry.
CHANGE_KINDS: tuple[str, ...] = (
    "added", "date_moved", "confidence_changed", "sources_added", "retired",
)
# The three that mean a new version is warranted. A source that repeated what
# the calendar already said, and an entry aging out, are not news.
PUBLISHING_CHANGES: frozenset[str] = frozenset({
    "added", "date_moved", "confidence_changed",
})

# How far apart two statements can be and still be about the same event.
#
# This is the number that decides whether "Yahoo says the 5th" and "the 8-K
# says the 2nd" are one occurrence disagreeing or two occurrences, and it is
# chosen to sit in the gap between the two things it has to tell apart. A
# rescheduled results date moves by days, occasionally by a fortnight. Two
# consecutive quarterly reports are seventy-seven to ninety-two days apart --
# Accenture's fiscal Q4 lands on 1 October and its Q1 in mid-December, which is
# seventy-seven. Forty-five is comfortably above the first and comfortably
# below the second.
#
# Getting this wrong in the generous direction is what a calendar quarter did:
# both of Accenture's autumn reports fall in calendar Q4, so keying on the
# quarter folded a confirmed past date and a forthcoming estimate into one
# entry, the confirmed one won because filings outrank vendors, and the
# December preview simply never happened. Erring the other way costs a visible
# duplicate on the strip, which somebody can see and fix.
SAME_OCCURRENCE_DAYS = 45

# How long a passed event stays on the current calendar. Long enough that the
# quarter just reported is still readable beside the one coming -- which is
# what a calibration wants -- and short enough that the entry list is about
# what is ahead. Aging out never publishes a version by itself.
MAX_PAST_DAYS = 120
# Ceilings, not expectations: one company's forthcoming events, and the number
# of independent sources that can speak to one of them.
MAX_ENTRIES = 200
MAX_SOURCES_PER_ENTRY = 8
MAX_NOTE_CHARS = 500

_SCHEMA_PATH = Path(__file__).with_name("catalyst_calendar_schema.sql")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

_SOURCE_FIELDS = frozenset({
    "kind", "ref", "source_ref", "observed_date", "observed_at", "confidence",
    "note",
})
_ENTRY_INPUT_FIELDS = frozenset({"event_kind", "sources", "notes"})


class CatalystCalendarError(RuntimeError):
    """Base error for the catalyst calendar authority."""


class CatalystCalendarValidationError(CatalystCalendarError, ValueError):
    """A request does not satisfy the closed contract."""


class CatalystCalendarConflict(CatalystCalendarError):
    """A request conflicts with the immutable chain."""


class CatalystCalendarNotFound(CatalystCalendarError):
    """No such calendar or version."""


# -- small validators ------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CatalystCalendarValidationError(f"{name} must be non-empty text")
    return value.strip()


def _optional_text(value: Any, name: str, *, limit: int = MAX_NOTE_CHARS) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CatalystCalendarValidationError(f"{name} must be text")
    value = value.strip()
    if len(value) > limit:
        raise CatalystCalendarValidationError(
            f"{name} is longer than {limit} characters"
        )
    return value


def _iso_date(value: Any, name: str) -> str:
    value = _text(value, name)
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise CatalystCalendarValidationError(f"{name} must be YYYY-MM-DD") from exc
    return value


def _rfc3339(value: Any, name: str) -> str:
    value = _text(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CatalystCalendarValidationError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise CatalystCalendarValidationError(f"{name} must carry a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _one_of(value: Any, allowed: Sequence[str], name: str) -> str:
    value = _text(value, name)
    if value not in allowed:
        raise CatalystCalendarValidationError(
            f"{name} must be one of {sorted(allowed)}"
        )
    return value


def _closed(value: Any, fields: frozenset[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CatalystCalendarValidationError(f"{name} must be an object")
    wire = dict(value)
    if set(wire) != set(fields):
        raise CatalystCalendarValidationError(
            f"{name} has invalid closed shape; "
            f"missing={sorted(set(fields) - set(wire))}, "
            f"unknown={sorted(set(wire) - set(fields))}"
        )
    return wire


def _record(base: Mapping[str, Any]) -> dict[str, Any]:
    wire = dict(base)
    wire["content_hash"] = content_hash(wire)
    return wire


def calendar_ref_for(company_ref: str) -> str:
    """One company, one calendar."""

    return f"catalyst-calendar:{_text(company_ref, 'company_ref')}"


def entry_ref_for(company_ref: str, event_kind: str, anchor_date: str) -> str:
    """The identity of one occurrence: where it was first seen to be.

    A date that moves has to stay the *same* entry, or a rescheduled call would
    read as a cancellation and a new event rather than as the one fact it is.
    So the ref is fixed at the first date anybody gave for this occurrence and
    never moves again, while ``expected_date`` does.

    Which occurrence a later statement belongs to is not decided here and is
    not a function of the date alone -- see :func:`_match`. Nothing outside
    this module can compute an entry ref for an occurrence it has not seen,
    which is the point: identity is the authority's to assign, and a source
    mapper that could name an occurrence could merge two of them.
    """

    return "catalyst-entry:" + content_hash({
        "company_ref": _text(company_ref, "company_ref"),
        "event_kind": _one_of(event_kind, EVENT_KINDS, "event_kind"),
        "anchor_date": _iso_date(anchor_date, "anchor_date"),
    })[:32]


def occurrence_label(anchor_date: str) -> str:
    """A short human name for an occurrence: ``2026q4``.

    A label and nothing more. It was the identity once, keyed on the calendar
    quarter, and that is exactly the bug this module now has a test for: two
    of Accenture's results announcements fall in the same calendar quarter.
    Kept because "ACN 2026q4" reads better on a strip than a hash, and demoted
    because two things that are not the same must not share a name that
    something might key on.
    """

    parsed = date.fromisoformat(_iso_date(anchor_date, "anchor_date"))
    return f"{parsed.year}q{(parsed.month - 1) // 3 + 1}"


# -- source and entry normalisation ----------------------------------------


def _normalise_source(raw: Any, name: str) -> dict[str, Any]:
    """One thing that said a date, and what kind of thing it is."""

    item = _closed(raw, _SOURCE_FIELDS, name)
    kind = _one_of(item["kind"], SOURCE_KINDS, f"{name}.kind")
    confidence = _one_of(item["confidence"], CONFIDENCES, f"{name}.confidence")
    derived = "confirmed" if kind in CONFIRMING_SOURCE_KINDS else "estimated"
    if confidence != derived:
        # Not a correction: a refusal. A caller that can label a vendor's guess
        # `confirmed` is a caller that can turn the whole distinction off, and
        # the distinction is the only reason there are two sources.
        raise CatalystCalendarConflict(
            f"{name} is a {kind} source, so its confidence is {derived!r} and "
            f"not {confidence!r}; only a company-issued source confirms a date"
        )
    return {
        "kind": kind,
        "ref": _text(item["ref"], f"{name}.ref"),
        "source_ref": _text(item["source_ref"], f"{name}.source_ref"),
        "observed_date": _iso_date(item["observed_date"], f"{name}.observed_date"),
        "observed_at": _rfc3339(item["observed_at"], f"{name}.observed_at"),
        "confidence": confidence,
        "note": _optional_text(item["note"], f"{name}.note"),
    }


def _source_slot(source: Mapping[str, Any]) -> tuple[str, str]:
    """What a second reading replaces, and what it sits beside.

    A vendor call keeps one slot per source: Yahoo re-read tomorrow is Yahoo,
    not a second witness. Counting each morning's call as its own source would
    let one vendor out-vote a filing by repetition, and would leave every entry
    permanently "in disagreement" with its own history.

    A filing keeps a slot per accession, because it is the opposite kind of
    thing: an 8-K is an immutable announcement, and two of them in one quarter
    are two announcements. IBM filed an Item 2.02 on 14 and again on 22 July
    2026; collapsing those into "SEC's current opinion" would silently drop
    one of two facts the company actually stated.
    """

    if source["kind"] in CONFIRMING_SOURCE_KINDS:
        return (source["kind"], source["ref"])
    return (source["kind"], source["source_ref"])


def _rank(source: Mapping[str, Any]) -> tuple[int, str, str]:
    """Highest authority first, then the most recent reading, then the ref."""

    return (
        -SOURCE_AUTHORITY.get(source["kind"], 0),
        # Descending on time without needing a reverse flag: the later a
        # reading is, the smaller this sorts.
        _invert(source["observed_at"]),
        source["ref"],
    )


def _invert(value: str) -> str:
    """Sort a fixed-alphabet timestamp descending inside an ascending key."""

    return "".join(chr(0x7E - ord(character)) for character in value)


# Why an entry asserts the date it does.
RESOLUTIONS: tuple[str, ...] = (
    # The highest-authority source that spoke to this occurrence.
    "highest_authority",
    # A source said this had already happened while another said it was still
    # ahead, and the one still ahead was taken. See :func:`_resolve`.
    "forthcoming_over_past",
)


def _resolve(entry_ref: str, event_kind: str, anchor_date: str,
             sources: Sequence[Mapping[str, Any]], notes: str,
             today: str) -> dict[str, Any]:
    """Build the entry the readers see from the sources that speak to it.

    Ordinarily the highest-authority source decides, which is how a filing
    beats a vendor. One case overrides that, and it is not a matter of
    authority at all: **a source saying an event has already happened may not
    decide the date of an occurrence some other source says is still ahead.**

    That case arises where two statements are close enough to be the same
    occurrence but land either side of today -- a confirmed release three weeks
    back beside a vendor that has already rolled its estimate forward. The
    filing outranks the vendor and is not wrong about anything; it is simply
    answering a different question. Letting it win made the entry's
    ``expected_date`` a past date, which took the occurrence out of
    ``next_catalyst`` entirely and silently cancelled the preview. The past
    statement stays in ``sources`` and in ``disagreeing_dates``; it just does
    not get to say when the next one is.
    """

    ordered = sorted(sources, key=_rank)
    if not ordered:
        raise CatalystCalendarValidationError(
            "an entry needs at least one source; a date nobody stated is a guess"
        )
    if len(ordered) > MAX_SOURCES_PER_ENTRY:
        raise CatalystCalendarConflict(
            f"an entry may carry at most {MAX_SOURCES_PER_ENTRY} live sources"
        )
    forthcoming = [item for item in ordered if item["observed_date"] >= today]
    if forthcoming and forthcoming[0] is not ordered[0]:
        winner, resolution = forthcoming[0], "forthcoming_over_past"
    else:
        winner, resolution = ordered[0], "highest_authority"
    dates = sorted({item["observed_date"] for item in ordered})
    return {
        "entry_ref": entry_ref,
        "event_kind": event_kind,
        "anchor_date": anchor_date,
        "subject": occurrence_label(anchor_date),
        "expected_date": winner["observed_date"],
        "confidence": winner["confidence"],
        "resolution": resolution,
        # Said out loud rather than left to be derived by a reader who may not
        # think to. Two sources that disagree about when a company reports is
        # exactly the thing a person should be shown.
        "disagreement": len(dates) > 1,
        "disagreeing_dates": dates if len(dates) > 1 else [],
        "sources": [dict(item) for item in ordered],
        "notes": notes,
    }


def _observation(raw: Any, index: int) -> dict[str, Any]:
    """One run's report about one occurrence, before it meets the chain.

    It names no occurrence, because it cannot know which one it is: that is
    decided by where its date falls relative to what the calendar already
    holds. What it carries is a kind, the sources that spoke, and their notes.
    """

    name = f"entries[{index}]"
    item = _closed(raw, _ENTRY_INPUT_FIELDS, name)
    event_kind = _one_of(item["event_kind"], EVENT_KINDS, f"{name}.event_kind")
    raw_sources = item["sources"]
    if not isinstance(raw_sources, (list, tuple)) or not raw_sources:
        raise CatalystCalendarValidationError(f"{name}.sources must be a non-empty array")
    sources = [
        _normalise_source(source, f"{name}.sources[{position}]")
        for position, source in enumerate(raw_sources)
    ]
    slots = [_source_slot(source) for source in sources]
    if len(set(slots)) != len(slots):
        raise CatalystCalendarValidationError(
            f"{name} names the same source twice; one reading per source"
        )
    return {
        "event_kind": event_kind,
        "sources": sources,
        "notes": _optional_text(item["notes"], f"{name}.notes"),
        # What this observation is about, for matching. The best-ranked source
        # it carries: a run that hands over a filing and a vendor reading at
        # once is about where the filing says it is.
        "observed_date": min(sources, key=_rank)["observed_date"],
    }


def _match(
    observed_date: str, event_kind: str, anchors: Mapping[str, tuple[str, str]]
) -> str | None:
    """Which occurrence a statement belongs to, or None for a new one.

    Nearest anchor of the same kind inside :data:`SAME_OCCURRENCE_DAYS`. Ties
    go to the earlier anchor so that two runs in any order agree.

    The anchor is used rather than the entry's current ``expected_date`` on
    purpose: matching against a value that moves lets an occurrence walk. Three
    reschedules of a fortnight each would carry an entry six weeks from where
    it started, and the next quarter's first estimate would then land inside
    the window of an entry that is no longer about it.
    """

    day = date.fromisoformat(observed_date)
    best: tuple[int, str, str] | None = None
    for ref, (kind, anchor) in anchors.items():
        if kind != event_kind:
            continue
        distance = abs((date.fromisoformat(anchor) - day).days)
        if distance > SAME_OCCURRENCE_DAYS:
            continue
        candidate = (distance, anchor, ref)
        if best is None or candidate < best:
            best = candidate
    return None if best is None else best[2]


# -- the chain -------------------------------------------------------------


def _decode(row: sqlite3.Row | None, name: str) -> dict[str, Any]:
    if row is None:
        raise CatalystCalendarNotFound(name)
    try:
        wire = json.loads(row["record_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise CatalystCalendarConflict(f"{name} record_json is invalid") from exc
    if not isinstance(wire, dict) or canonical_json(wire) != row["record_json"]:
        raise CatalystCalendarConflict(f"{name} record_json is not canonical")
    body = dict(wire)
    asserted = body.pop("content_hash", None)
    if asserted != content_hash(body) or asserted != row["content_hash"]:
        raise CatalystCalendarConflict(f"{name} content hash drifted")
    if wire.get("id") != row["version_id"]:
        raise CatalystCalendarConflict(f"{name} identity column drifted")
    checks = {
        "calendar_ref": wire.get("calendar_ref"),
        "version_number": wire.get("version"),
        "prior_version_id": wire.get("prior_version_ref"),
        "company_ref": wire.get("company_ref"),
        "entry_count": len(wire.get("entries") or []),
        "change_reason": wire.get("change_reason"),
        "next_catalyst_date": wire.get("next_catalyst_date"),
        "actor_ref": wire.get("actor_ref"),
        "created_at": wire.get("created_at"),
    }
    for column, expected in checks.items():
        if column in row.keys() and row[column] != expected:
            raise CatalystCalendarConflict(f"{name} {column} drifted")
    return wire


def required_change_reason(changes: Sequence[Mapping[str, Any]]) -> str:
    """The reason ADR-0008 says this diff has, derived from the diff itself.

    A moved date is something that happened in the world, so it is a
    ``driver_event``; an estimate that became confirmed is the same date known
    better, so it is ``evidence_thicker``. Deriving it means a version cannot
    be labelled with a reason its own contents contradict, which is the only
    way a chain of reasons stays worth reading.
    """

    kinds = {change["change"] for change in changes}
    if "date_moved" in kinds:
        return "driver_event"
    return "evidence_thicker"


class CatalystCalendarAuthority:
    """Append-only dated-event calendar, one version chain per company."""

    def __init__(self, store: Any):
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("CatalystCalendarAuthority requires a DaltonStore")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    # -- reading -----------------------------------------------------------

    def version(self, version_ref: str) -> dict[str, Any]:
        return self._decode_version(self.connection, _text(version_ref, "version_ref"))

    @staticmethod
    def _decode_version(cursor: Any, version_ref: str) -> dict[str, Any]:
        row = cursor.execute(
            "SELECT * FROM catalyst_calendar_versions WHERE version_id=?",
            (version_ref,),
        ).fetchone()
        return _decode(row, f"CatalystCalendarVersion {version_ref}")

    def latest_version(self, company_ref: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM catalyst_calendar_versions WHERE calendar_ref=? "
            "ORDER BY version_number DESC LIMIT 1",
            (calendar_ref_for(company_ref),),
        ).fetchone()
        if row is None:
            return None
        return _decode(row, f"latest CatalystCalendarVersion for {company_ref}")

    def versions(self, company_ref: str) -> list[dict[str, Any]]:
        """The whole chain, oldest first. A rescheduled call is visible here."""

        rows = self.connection.execute(
            "SELECT * FROM catalyst_calendar_versions WHERE calendar_ref=? "
            "ORDER BY version_number ASC",
            (calendar_ref_for(company_ref),),
        ).fetchall()
        return [
            _decode(row, f"CatalystCalendarVersion for {company_ref}") for row in rows
        ]

    def entries(self, company_ref: str) -> list[dict[str, Any]]:
        latest = self.latest_version(company_ref)
        return [] if latest is None else [dict(entry) for entry in latest["entries"]]

    def next_catalyst(
        self, company_ref: str, now: datetime | date | None = None
    ) -> dict[str, Any] | None:
        """The soonest thing that has not happened yet, or None.

        Today counts as forthcoming: a company reporting this morning is the
        most relevant catalyst there is, and the calibration window opens on
        the day itself.
        """

        today = _as_date(now)
        latest = self.latest_version(company_ref)
        if latest is None:
            return None
        forthcoming = [
            entry for entry in latest["entries"] if entry["expected_date"] >= today
        ]
        if not forthcoming:
            return None
        entry = min(
            forthcoming,
            key=lambda item: (
                item["expected_date"], item["event_kind"], item["anchor_date"]),
        )
        return self._reader_view(latest, entry, today)

    def upcoming(
        self, now: datetime | date | None = None, horizon_days: int = 45
    ) -> list[dict[str, Any]]:
        """Every company's forthcoming entries inside the horizon, by date."""

        today = _as_date(now)
        if not isinstance(horizon_days, int) or isinstance(horizon_days, bool):
            raise CatalystCalendarValidationError("horizon_days must be an integer")
        if horizon_days < 0:
            raise CatalystCalendarValidationError("horizon_days must not be negative")
        limit = (date.fromisoformat(today) + timedelta(days=horizon_days)).isoformat()
        rows = self.connection.execute(
            "SELECT * FROM catalyst_calendar_versions AS outer WHERE version_number=("
            "SELECT MAX(version_number) FROM catalyst_calendar_versions AS inner "
            "WHERE inner.calendar_ref=outer.calendar_ref) ORDER BY calendar_ref"
        ).fetchall()
        found: list[dict[str, Any]] = []
        for row in rows:
            version = _decode(row, f"CatalystCalendarVersion {row['version_id']}")
            for entry in version["entries"]:
                if today <= entry["expected_date"] <= limit:
                    found.append(self._reader_view(version, entry, today))
        found.sort(key=lambda item: (
            item["expected_date"], item["company_ref"], item["event_kind"],
            item["anchor_date"],
        ))
        return found

    @staticmethod
    def _reader_view(
        version: Mapping[str, Any], entry: Mapping[str, Any], today: str
    ) -> dict[str, Any]:
        """One entry as a caller should show it, caveat included.

        ``date_caveat`` is carried here as well as on the event for the same
        reason it is carried there: every consumer of this reader is something
        that puts a date in front of a person, and "T-22 days" next to a date a
        vendor guessed reads exactly like "T-22 days" next to one the company
        announced unless something says otherwise.
        """

        unconfirmed = entry["confidence"] != "confirmed"
        return {
            **{key: value for key, value in entry.items()},
            "company_ref": version["company_ref"],
            "version_ref": version["id"],
            "version_hash": version["content_hash"],
            "version": version["version"],
            "date_unconfirmed": unconfirmed,
            "date_caveat": UNCONFIRMED_DATE_CAVEAT if unconfirmed else "",
            "days_until": (
                date.fromisoformat(entry["expected_date"]) - date.fromisoformat(today)
            ).days,
        }

    # -- publishing --------------------------------------------------------

    def publish(
        self,
        *,
        company_ref: str,
        entries: Sequence[Mapping[str, Any]],
        change_reason: str,
        evidence_refs: Sequence[str],
        actor_ref: str = ACTOR_REF,
        now: datetime | date | None = None,
    ) -> dict[str, Any]:
        """Merge one run's observations into the chain, or say it learned nothing.

        ``entries`` is what *this run* saw, not the whole calendar. A yfinance
        run knows nothing about the 8-K the SEC detector found last week, and a
        publish that replaced the entry set wholesale would drop the confirmed
        date every time the vendor was polled.

        The whole read-merge-write is one transaction, for the same reason the
        price series does it: two children publishing the same company would
        otherwise both compute version N and the second would surface a bare
        ``IntegrityError``.
        """

        company_ref = _text(company_ref, "company_ref")
        actor_ref = _text(actor_ref, "actor_ref")
        change_reason = _one_of(change_reason, CHANGE_REASONS, "change_reason")
        if not isinstance(entries, (list, tuple)):
            raise CatalystCalendarValidationError("entries must be an array")
        if len(entries) > MAX_ENTRIES:
            raise CatalystCalendarValidationError(
                f"a run may carry at most {MAX_ENTRIES} entries"
            )
        if not isinstance(evidence_refs, (list, tuple)) or not evidence_refs:
            # ADR-0008: a version that cannot name what it learned is a rewrite.
            raise CatalystCalendarValidationError(
                "a version needs the evidence refs that occasioned it"
            )
        refs = [_text(item, "evidence_refs[]") for item in evidence_refs]
        if len(set(refs)) != len(refs):
            raise CatalystCalendarValidationError("evidence_refs are not unique")
        # Two observations in one run about the same occurrence are normal and
        # are merged, not refused: the vendor half and the filed half of a
        # calendar run are two statements about the same earnings date, and it
        # is this module's job to notice that rather than the caller's.
        observed = [_observation(item, index) for index, item in enumerate(entries)]
        today = _as_date(now)
        with self.store._transaction() as cur:
            return self._merge_and_insert(
                cur, company_ref=company_ref, observed=observed,
                change_reason=change_reason, evidence_refs=sorted(refs),
                actor_ref=actor_ref, today=today,
            )

    def _merge_and_insert(
        self, cur: sqlite3.Cursor, *, company_ref: str,
        observed: list[dict[str, Any]], change_reason: str,
        evidence_refs: list[str], actor_ref: str, today: str,
    ) -> dict[str, Any]:
        calendar_ref = calendar_ref_for(company_ref)
        row = cur.execute(
            "SELECT * FROM catalyst_calendar_versions WHERE calendar_ref=? "
            "ORDER BY version_number DESC LIMIT 1",
            (calendar_ref,),
        ).fetchone()
        latest = None if row is None else _decode(
            row, f"latest CatalystCalendarVersion for {company_ref}")
        prior = {
            entry["entry_ref"]: dict(entry)
            for entry in (latest["entries"] if latest else [])
        }

        merged: dict[str, dict[str, Any]] = dict(prior)
        # Every occurrence this calendar knows about, by the date it was first
        # seen at. Grows as a run introduces new ones, so two statements in the
        # same run that are about the same event find each other.
        anchors: dict[str, tuple[str, str]] = {
            ref: (entry["event_kind"], entry["anchor_date"])
            for ref, entry in prior.items()
        }
        live_sources: dict[str, dict[tuple[str, str], dict[str, Any]]] = {
            ref: {_source_slot(source): dict(source) for source in entry["sources"]}
            for ref, entry in prior.items()
        }
        changes: list[dict[str, Any]] = []
        touched: dict[str, bool] = {}
        for item in observed:
            entry_ref = _match(item["observed_date"], item["event_kind"], anchors)
            if entry_ref is None:
                anchor = item["observed_date"]
                entry_ref = entry_ref_for(company_ref, item["event_kind"], anchor)
                anchors[entry_ref] = (item["event_kind"], anchor)
                live_sources.setdefault(entry_ref, {})
            live = live_sources.setdefault(entry_ref, {})
            for source in item["sources"]:
                slot = _source_slot(source)
                standing = live.get(slot)
                if standing is None:
                    live[slot] = source
                    touched[entry_ref] = True
                    continue
                if standing["observed_at"] > source["observed_at"]:
                    # An older reading of a source we already have a newer one
                    # of. Replaying a captured artifact should not walk the
                    # calendar backwards.
                    continue
                if standing != source:
                    touched[entry_ref] = True
                live[slot] = source
            before = prior.get(entry_ref)
            merged[entry_ref] = _resolve(
                entry_ref, item["event_kind"], anchors[entry_ref][1],
                list(live.values()),
                item["notes"] or (before or {}).get("notes", ""),
                today,
            )
        for entry_ref, added_source in touched.items():
            after, before = merged[entry_ref], prior.get(entry_ref)
            if before is None:
                changes.append(_change(after, "added", None, after["expected_date"]))
                continue
            if before["expected_date"] != after["expected_date"]:
                changes.append(_change(
                    after, "date_moved", before["expected_date"],
                    after["expected_date"],
                ))
            if before["confidence"] != after["confidence"]:
                changes.append(_change(
                    after, "confidence_changed", before["confidence"],
                    after["confidence"],
                ))
            if added_source and not any(
                change["entry_ref"] == entry_ref
                and change["change"] in PUBLISHING_CHANGES
                for change in changes
            ):
                changes.append(_change(after, "sources_added", None, None))

        if not any(change["change"] in PUBLISHING_CHANGES for change in changes):
            # Nothing this run saw was new. The source repeating itself is the
            # normal case for a calendar -- a date is announced once and then
            # simply stays true -- so the resting state of this lane is a read
            # that publishes nothing.
            if latest is None:
                raise CatalystCalendarValidationError(
                    "a first version needs at least one entry"
                )
            return {"status": "duplicate", **latest}

        # Aging out happens only alongside a real change, so it can never be
        # the reason a version exists. The entry is not lost: the version
        # before this one still holds it, and that version is never deleted.
        floor = (date.fromisoformat(today) - timedelta(days=MAX_PAST_DAYS)).isoformat()
        for entry_ref in sorted(merged):
            if merged[entry_ref]["expected_date"] < floor:
                changes.append(_change(merged.pop(entry_ref), "retired", None, None))
        if len(merged) > MAX_ENTRIES:
            raise CatalystCalendarConflict(
                f"this calendar would exceed {MAX_ENTRIES} entries"
            )

        derived = required_change_reason(changes)
        if change_reason not in (derived, "human_revision"):
            raise CatalystCalendarConflict(
                f"this diff is a {derived!r} and was offered as "
                f"{change_reason!r}; a version's reason has to be the one its "
                "own contents show"
            )

        ordered = [
            merged[key] for key in sorted(
                merged,
                key=lambda ref: (
                    merged[ref]["expected_date"], merged[ref]["event_kind"],
                    merged[ref]["anchor_date"],
                ),
            )
        ]
        changes.sort(key=lambda change: (change["entry_ref"], change["change"]))
        forthcoming = [
            entry["expected_date"] for entry in ordered
            if entry["expected_date"] >= today
        ]
        version = 1 if latest is None else latest["version"] + 1
        prior_version_ref = None if latest is None else latest["id"]
        identity = {
            "calendar_ref": calendar_ref,
            "version": version,
            "prior_version_ref": prior_version_ref,
            "company_ref": company_ref,
            "entries": ordered,
        }
        version_id = "catalyst-calendar-version:" + content_hash(identity)[:32]
        wire = _record({
            "schema_version": SCHEMA_VERSION,
            "id": version_id,
            "created_at": _now(),
            **identity,
            "entry_count": len(ordered),
            "change_reason": change_reason,
            "evidence_refs": evidence_refs,
            # Named rather than counted, for the same reason the price series
            # names its restated bars: a reader who has to diff two versions to
            # find out what moved will not do it.
            "changes": changes,
            "next_catalyst_date": min(forthcoming) if forthcoming else None,
            "as_of": today,
            "actor_ref": actor_ref,
        })
        try:
            cur.execute(
                "INSERT INTO catalyst_calendar_versions "
                "(version_id,calendar_ref,version_number,prior_version_id,"
                "company_ref,entry_count,change_reason,next_catalyst_date,"
                "record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    version_id, calendar_ref, version, prior_version_ref,
                    company_ref, wire["entry_count"], change_reason,
                    wire["next_catalyst_date"], canonical_json(wire),
                    wire["content_hash"], actor_ref, wire["created_at"],
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise CatalystCalendarConflict(
                "another run published this company's next version first"
            ) from exc
        stored = self._decode_version(cur, version_id)
        if stored != wire:
            raise CatalystCalendarConflict(
                "stored catalyst calendar version does not read back"
            )
        return {"status": "fresh", **stored}


def _change(
    entry: Mapping[str, Any], kind: str, before: Any, after: Any
) -> dict[str, Any]:
    return {
        "entry_ref": entry["entry_ref"],
        "event_kind": entry["event_kind"],
        "subject": entry["subject"],
        "anchor_date": entry["anchor_date"],
        "change": _one_of(kind, CHANGE_KINDS, "change"),
        "from": before,
        "to": after,
    }


def _as_date(value: datetime | date | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).date().isoformat()
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return _iso_date(value, "now")


# -- events ----------------------------------------------------------------
#
# The P14f windows, and the only thing this lane says to the rest of the
# system. Kept in this module and not in the lane so that it can be tested
# against a list of entries with no store, no launcher and no subprocess.

# A month before the report: blueprint P14f's preview window.
PREVIEW_LEAD_DAYS = 30
# T+0 through T+2: the calibration has to be written while the call is still
# what everyone is talking about.
CALIBRATION_TRAILING_DAYS = 2
# Which kinds of event get a preview and a calibration at all. A dividend going
# ex is a date to know and not a piece of work; a results announcement, a
# guidance update and an investor day each produce something to read.
WINDOWED_EVENT_KINDS: tuple[str, ...] = ("earnings", "guidance", "investor_day")
EVENT_WINDOWS: tuple[str, ...] = ("preview", "calibration", "date_change")
# Windows an unconfirmed date may open.
#
# The preview may, and this is how the job is actually done: an analyst writes
# the preview against the expected date and does not wait for the company to
# confirm it, because by the time the confirmation arrives most of the month
# is gone. What that costs is a preview occasionally built against a date that
# then moves, and the answer to that is to say which kind of date it was
# rather than to not do the work.
#
# The calibration may not. It is written *about* a release, and until the
# company has reported there is nothing to calibrate against; an estimated
# date says a report is likely, not that one happened.
UNCONFIRMED_DATE_WINDOWS: frozenset[str] = frozenset({"preview", "date_change"})
# What a renderer puts next to an unconfirmed date. Carried on the event rather
# than left to each consumer to invent, so the caveat cannot be dropped by the
# one place that forgets it.
UNCONFIRMED_DATE_CAVEAT = "日期未确认"
# What ``kind`` these go into the P14a event ledger as.
RESEARCH_EVENT_KIND = "calendar"


def calendar_event_key(
    company_ref: str, entry_ref: str, window: str, expected_date: str,
    confidence: str,
) -> str:
    """One name for this company, this occurrence, this window, this date, as
    well as it was known at the time.

    Deterministic on purpose. The lane runs daily and a preview window is open
    for a month, so something has to stop thirty identical events; keying on
    the date means a call that moves genuinely reopens the window rather than
    being suppressed by the one already emitted.

    ``confidence`` is in the key for the same reason the date is. A preview
    fired against an estimated date and the same preview once the company has
    confirmed it are two different pieces of news: the second one tells the
    judgement layer the work it planned is now safe to commit to, and keying
    without it would swallow that.
    """

    return "calendar-event:" + content_hash({
        "company_ref": _text(company_ref, "company_ref"),
        "entry_ref": _text(entry_ref, "entry_ref"),
        "window": _one_of(window, EVENT_WINDOWS, "window"),
        "expected_date": _iso_date(expected_date, "expected_date"),
        "confidence": _one_of(confidence, CONFIDENCES, "confidence"),
    })[:32]


def emit_calendar_events(
    *,
    company_ref: str,
    entries: Sequence[Mapping[str, Any]],
    record_event: Callable[..., Any],
    now: datetime | date | None = None,
    version_ref: str | None = None,
    moved_entry_refs: Sequence[str] = (),
    is_emitted: Callable[[str], bool] | None = None,
) -> list[dict[str, Any]]:
    """Tell the event ledger which windows opened, through the caller's writer.

    ``record_event`` is a parameter rather than an import because P14a owns
    that module and this one must not reach into it: the integrator passes
    ``research_event.record_event`` and the tests pass a fake that records what
    it was asked to write.

    **An estimated date opens the preview; only a confirmed one opens the
    calibration.** This is how the job is done: the preview is written against
    the expected date, because waiting for the company to confirm spends most
    of the month one is preparing in. What that costs is the occasional preview
    built against a date that then moves, and the answer to that is to say
    which kind of date it was -- ``date_confidence`` and a caveat travel on the
    event and on anything that renders it -- rather than to skip the work.

    The confirmation is then its own event. When the company's own filing turns
    an estimate into a confirmed date, the entry's version chain records
    ``evidence_thicker`` and a second ``preview`` event goes out, so the
    judgement layer can re-plan against a date that is now safe to commit to.
    That works because ``confidence`` is part of the event key.

    The calibration is confirmed-only and stays that way. It is written *about*
    a release; an estimated date says a report was likely, not that one
    happened, and a calibration of a call nobody made is not a thin answer but
    a wrong one.

    A date that *moved* is emitted whatever its confidence, because "Yahoo now
    thinks a week later" is a fact about the world's expectations even when it
    is not a fact about the company -- and the judgement layer, not this
    function, decides what it is worth.
    """

    today = _as_date(now)
    moved = {str(ref) for ref in moved_entry_refs}
    emitted: list[dict[str, Any]] = []
    for entry in entries:
        expected = entry["expected_date"]
        confidence = entry["confidence"]
        unconfirmed = confidence != "confirmed"
        days_until = (
            date.fromisoformat(expected) - date.fromisoformat(today)
        ).days
        windows: list[str] = []
        if entry["entry_ref"] in moved:
            windows.append("date_change")
        if entry["event_kind"] in WINDOWED_EVENT_KINDS:
            if 0 < days_until <= PREVIEW_LEAD_DAYS:
                windows.append("preview")
            elif -CALIBRATION_TRAILING_DAYS <= days_until <= 0:
                windows.append("calibration")
        if unconfirmed:
            windows = [
                window for window in windows if window in UNCONFIRMED_DATE_WINDOWS
            ]
        for window in windows:
            key = calendar_event_key(
                company_ref, entry["entry_ref"], window, expected, confidence
            )
            if is_emitted is not None and is_emitted(key):
                continue
            payload = {
                "event_key": key,
                "window": window,
                "entry_ref": entry["entry_ref"],
                "event_kind": entry["event_kind"],
                # A label and an identity. Two occurrences can share the
                # label -- Accenture reports twice in calendar Q4 -- so
                # anything keying on one of these has to key on the ref.
                "subject": entry["subject"],
                "anchor_date": entry["anchor_date"],
                "expected_date": expected,
                # Named for what it qualifies. An event ledger will hold other
                # confidences before long, and a bare "confidence" beside a
                # date is the kind of field a renderer attaches to the wrong
                # thing.
                "date_confidence": confidence,
                "date_unconfirmed": unconfirmed,
                # The words to put beside the date, so the caveat cannot be
                # lost by whichever consumer forgets to derive it.
                "date_caveat": UNCONFIRMED_DATE_CAVEAT if unconfirmed else "",
                "disagreement": entry["disagreement"],
                "disagreeing_dates": list(entry.get("disagreeing_dates") or []),
                "days_until": days_until,
                "as_of": today,
            }
            source_refs = [source["ref"] for source in entry["sources"]]
            if version_ref:
                source_refs.append(version_ref)
            record_event(
                company_ref=company_ref,
                kind=RESEARCH_EVENT_KIND,
                occurred_at=f"{today}T00:00:00+00:00",
                source_refs=source_refs,
                payload=payload,
            )
            emitted.append(payload)
    emitted.sort(key=lambda item: (item["expected_date"], item["window"]))
    return emitted


__all__ = [
    "ACTOR_REF",
    "CALIBRATION_TRAILING_DAYS",
    "CHANGE_KINDS",
    "CHANGE_REASONS",
    "CONFIDENCES",
    "CONFIRMING_SOURCE_KINDS",
    "EVENT_KINDS",
    "EVENT_WINDOWS",
    "MAX_ENTRIES",
    "MAX_PAST_DAYS",
    "MAX_SOURCES_PER_ENTRY",
    "PREVIEW_LEAD_DAYS",
    "PUBLISHING_CHANGES",
    "RESOLUTIONS",
    "SAME_OCCURRENCE_DAYS",
    "RESEARCH_EVENT_KIND",
    "UNCONFIRMED_DATE_CAVEAT",
    "UNCONFIRMED_DATE_WINDOWS",
    "SCHEMA_VERSION",
    "SOURCE_AUTHORITY",
    "SOURCE_KINDS",
    "WINDOWED_EVENT_KINDS",
    "CatalystCalendarAuthority",
    "CatalystCalendarConflict",
    "CatalystCalendarError",
    "CatalystCalendarNotFound",
    "CatalystCalendarValidationError",
    "calendar_event_key",
    "calendar_ref_for",
    "emit_calendar_events",
    "entry_ref_for",
    "occurrence_label",
    "required_change_reason",
]
