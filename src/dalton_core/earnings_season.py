"""P14f: the two windows of an earnings season, and everything deterministic.

An analyst's year has a shape.  A month before a company reports, a preview
goes out: what we model for the quarter, what the street is at, what the
company itself guided to, what we are watching and what would confirm or break
the thesis.  Within two days of the print, a calibration goes out: what came
in inline, better or worse, why, and what -- if anything -- that says about the
thesis.  Neither of them decides anything.  The preview is a piece of work to
read; the calibration is a proposal a person rules on once.

This module is the half of that with no model call in it: which occurrence is
open, whether it has already been written about, which fiscal period the
report is about, what our own model says about that period, what the guidance
profile said and what answered it.  It is kept separate from the two window
modules so that all of it can be tested against dictionaries -- no store, no
subprocess, no broker.

Three rules are load-bearing and are enforced here rather than left to the
caller:

* **An estimated date opens the preview; only a confirmed one opens the
  calibration.**  C1 decided this and the reason is the same one an analyst
  gives: you write the preview against the expected date because waiting spends
  the month you are preparing in, but a calibration is written *about* a
  release, and there is nothing to calibrate until one has happened.  We
  re-check it here because a lane that skipped the check would produce a
  calibration of a call nobody made, which is not a thin answer but a wrong one.
* **``date_confidence`` travels with everything an unconfirmed preview
  produces.**  Carried from the event rather than re-derived, so the one
  renderer that forgets cannot drop it.
* **One piece of work per occurrence.**  Not per event: C1 emits a second
  preview event when the company confirms a date it had estimated, and that is
  news for the judgement layer's schedule, not a reason to pay for a second
  preview.  The idempotency key is the occurrence.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from typing import Any, Callable

from .catalyst_calendar import (
    CALIBRATION_TRAILING_DAYS,
    PREVIEW_LEAD_DAYS,
    UNCONFIRMED_DATE_CAVEAT,
)
from .cockpit_model import register_purpose
from .store import content_hash

SCHEMA_VERSION = "0.1"

# The two deliverable kinds this slice adds, and the two model purposes.  Both
# purposes are already seeded as ``brain`` tier in ``model_fallback_chain``:
# a preview read by a person and a calibration that proposes a thesis change
# are not work for the cheap tier.
PREVIEW_KIND = "earnings_preview"
CALIBRATION_KIND = "earnings_calibration"
PREVIEW_PURPOSE = register_purpose(PREVIEW_KIND)
CALIBRATION_PURPOSE = register_purpose(CALIBRATION_KIND)

# What this slice emits into P14a's ledger when a calibration is written.  It is
# not a ``reconciliation`` event -- those are one per metric row and are
# already emitted by P14a's own scan -- but the one sentence the judgement
# lane needs: this occurrence has been calibrated, the history has been
# actualised, and *the forward periods have not been touched*.  Whether the
# print changes next year is a judgement, and this slice does not make it.
CALIBRATION_EVENT_KIND = "calibration"

WINDOWS: tuple[str, ...] = ("preview", "calibration")
# Only a confirmed date opens this one.  Kept here as well as in C1 because a
# lane that reads events from a ledger cannot assume the emitter that wrote
# them was the emitter it was written against.
CONFIRMED_ONLY_WINDOWS: frozenset[str] = frozenset({"calibration"})
CONFIRMED = "confirmed"

# How far a calibration may be written after the print.  The blueprint says 48
# hours and means it: a calibration written a week later is a memo, not a
# reaction, and the judgement it feeds has already been made by hand.
CALIBRATION_DEADLINE_DAYS = CALIBRATION_TRAILING_DAYS

MAX_FORECAST_ROWS = 12
MAX_GUIDANCE_ROWS = 8
MAX_CONSENSUS_ROWS = 8
MAX_WATCH_ROWS = 6
MAX_THESES = 4


class EarningsSeasonError(RuntimeError):
    """Base error for the earnings-season windows."""


class EarningsSeasonValidationError(EarningsSeasonError, ValueError):
    """A request does not satisfy the closed contract."""


class EarningsSeasonRefused(EarningsSeasonError):
    """A window was asked for that this occurrence may not open."""


def _today(now: datetime | date | None) -> str:
    if now is None:
        return datetime.now(timezone.utc).date().isoformat()
    if isinstance(now, datetime):
        moment = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).date().isoformat()
    if isinstance(now, date):
        return now.isoformat()
    return str(now)[:10]


def _text(value: Any, name: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EarningsSeasonValidationError(f"{name} must be non-empty text")
    return value.strip()[:maximum]


# ---------------------------------------------------------------------------
# reading a calendar event
# ---------------------------------------------------------------------------
#
# C1's emitter writes a rich payload -- ``window``, ``entry_ref``,
# ``anchor_date``, ``date_confidence``, ``date_caveat``.  The event ledger's
# own declared contract for ``kind: calendar`` is narrower:
# ``{event_kind, expected_date, confirmed, calendar_version_ref, source_ref}``.
# The two have not been reconciled yet (the report says so, and names it as an
# integration to-do), and this slice must work either way: it reads what is there
# and derives what is not, rather than depending on whichever emitter won.


def date_confidence_of(payload: Mapping[str, Any]) -> str:
    """``confirmed`` / ``estimated``, from whichever field carries it."""

    stated = payload.get("date_confidence")
    if isinstance(stated, str) and stated.strip():
        return stated.strip()
    return CONFIRMED if payload.get("confirmed") else "estimated"


def window_of(
    event: Mapping[str, Any], *, now: datetime | date | None = None
) -> str | None:
    """Which P14f window this calendar event opens, or nothing.

    Prefers the emitter's own word.  Derives it from the date when the event
    was written under the ledger's narrower contract, using C1's windows so
    the two paths cannot drift apart into two different definitions of "a
    month before".
    """

    payload = event.get("payload") or {}
    stated = payload.get("window")
    if isinstance(stated, str) and stated in WINDOWS:
        window = stated
    elif isinstance(stated, str) and stated:
        # ``date_change`` and anything C1 adds later: a fact about the
        # calendar, not a window this slice opens.
        return None
    else:
        expected = payload.get("expected_date")
        if not isinstance(expected, str) or not expected.strip():
            return None
        try:
            days_until = (
                date.fromisoformat(expected.strip())
                - date.fromisoformat(_today(now))
            ).days
        except ValueError:
            return None
        if 0 < days_until <= PREVIEW_LEAD_DAYS:
            window = "preview"
        elif -CALIBRATION_TRAILING_DAYS <= days_until <= 0:
            window = "calibration"
        else:
            return None
    if window in CONFIRMED_ONLY_WINDOWS and date_confidence_of(payload) != CONFIRMED:
        return None
    return window


def occurrence_ref(company_ref: str, entry_ref: str, anchor_date: str) -> str:
    """One name for one company reporting once.

    Not a function of the expected date: a date that moves is the same
    occurrence, and keying the work on the date would buy a second preview
    every time Yahoo changed its mind.
    """

    return "earnings-occurrence:" + content_hash({
        "company_ref": _text(company_ref, "company_ref"),
        "entry_ref": _text(entry_ref, "entry_ref"),
        "anchor_date": _text(anchor_date, "anchor_date"),
    })[:32]


def occurrence_of(
    event: Mapping[str, Any], *, now: datetime | date | None = None
) -> dict[str, Any] | None:
    """The occurrence a calendar event names, or nothing if it opens no window."""

    window = window_of(event, now=now)
    if window is None:
        return None
    payload = event.get("payload") or {}
    company_ref = _text(event["company_ref"], "company_ref")
    expected = _text(payload.get("expected_date"), "expected_date")
    event_kind = str(payload.get("event_kind") or "earnings")
    anchor = str(payload.get("anchor_date") or expected)
    entry = str(payload.get("entry_ref") or f"{company_ref}:{event_kind}:{anchor}")
    confidence = date_confidence_of(payload)
    unconfirmed = confidence != CONFIRMED
    return {
        "occurrence_ref": occurrence_ref(company_ref, entry, anchor),
        "company_ref": company_ref,
        "entry_ref": entry,
        "event_ref": event["id"],
        "event_hash": event.get("content_hash"),
        "event_kind": event_kind,
        "window": window,
        "anchor_date": anchor,
        "expected_date": expected,
        "date_confidence": confidence,
        "date_unconfirmed": unconfirmed,
        # Carried, not derived by each renderer.  A preview written against a
        # date the company has not confirmed says so on its face.
        "date_caveat": UNCONFIRMED_DATE_CAVEAT if unconfirmed else "",
        "source_refs": list(event.get("source_refs") or []),
        "occurred_at": event.get("occurred_at"),
        "as_of": _today(now),
    }


def deliverable_kind_for(window: str) -> str:
    if window == "preview":
        return PREVIEW_KIND
    if window == "calibration":
        return CALIBRATION_KIND
    raise EarningsSeasonValidationError(f"{window!r} is not a P14f window")


def idempotency_key_for(window: str, occurrence: Mapping[str, Any]) -> str:
    """One key per occurrence per window.

    Deliberately not a function of ``date_confidence``: a second preview event
    once the company confirms the date is a scheduling fact for the judgement
    layer, not a second preview.
    """

    return f"{deliverable_kind_for(window)}:{occurrence['occurrence_ref']}"


def already_written(
    connection: sqlite3.Connection, window: str, occurrence: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The deliverable this occurrence already produced for this window, if any.

    Read straight off the deliverable ledger rather than kept in a lane's
    memory: the lane is one process and the ledger outlives it, and the
    idempotency key is exactly what ``publish`` writes.
    """

    key = idempotency_key_for(window, occurrence)
    try:
        row = connection.execute(
            "SELECT version_id, record_json FROM mission_deliverable_versions "
            "WHERE kind=? AND subject_ref=? "
            "AND json_extract(record_json,'$.idempotency_key')=? "
            "ORDER BY version_number DESC LIMIT 1",
            (deliverable_kind_for(window), occurrence["company_ref"], key),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return None
    if row is None:
        return None
    try:
        return json.loads(row["record_json"])
    except (TypeError, json.JSONDecodeError):
        return {"id": row["version_id"]}


def open_occurrences(
    connection: sqlite3.Connection,
    events: Any,
    *,
    company_refs: Sequence[str],
    now: datetime | date | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Every occurrence with a window open and nothing written for it yet.

    Queueless, like the lanes around it: what is to be done is derived from the
    two ledgers each tick.  Newest event first within a company, because a
    calendar that moved should be read at its latest position.
    """

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for company_ref in company_refs:
        for event in events.events(
            company_ref=company_ref, kind="calendar", limit=40
        ):
            occurrence = occurrence_of(event, now=now)
            if occurrence is None:
                continue
            key = (occurrence["occurrence_ref"], occurrence["window"])
            if key in seen:
                continue
            seen.add(key)
            if already_written(connection, occurrence["window"], occurrence):
                continue
            out.append(occurrence)
            if len(out) >= limit:
                return out
    return out


def occurrences_from_calendar(
    connection: sqlite3.Connection,
    calendar: Any,
    *,
    company_refs: Sequence[str],
    now: datetime | date | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """The same list, derived from C1's calendar instead of the event ledger.

    Two reasons this exists beside :func:`open_occurrences`.  The first is that
    the wiring between C1 and the event ledger is not finished: the lane looks
    for a ``record_research_event`` the writer does not have, and C1's payload
    is wider than the ledger's declared ``calendar`` contract, so today no
    calendar event reaches the ledger at all.  The second is that it is the
    honest read for a *read-only* smoke: it answers "what would fire today"
    without writing an event to find out.

    It calls C1's own emitter with a collector in place of the writer, so the
    window rules are C1's and not a second copy of them here.  Nothing is
    written.
    """

    from .catalyst_calendar import emit_calendar_events

    out: list[dict[str, Any]] = []
    for company_ref in company_refs:
        try:
            entries = calendar.entries(company_ref)
        except Exception:  # noqa: BLE001 - a company with no calendar has no windows
            continue
        if not entries:
            continue
        collected: list[dict[str, Any]] = []

        def collect(**kwargs: Any) -> None:
            collected.append(dict(kwargs))

        emit_calendar_events(
            company_ref=company_ref, entries=entries, record_event=collect, now=now,
        )
        for call in collected:
            event = {
                "id": "calendar-window:" + content_hash({
                    "company_ref": company_ref,
                    "payload": dict(call.get("payload") or {}),
                })[:32],
                "company_ref": company_ref,
                "content_hash": None,
                "kind": "calendar",
                "occurred_at": call.get("occurred_at"),
                "source_refs": list(call.get("source_refs") or ()),
                "payload": dict(call.get("payload") or {}),
            }
            occurrence = occurrence_of(event, now=now)
            if occurrence is None:
                continue
            occurrence["derived_from"] = "catalyst_calendar"
            if already_written(connection, occurrence["window"], occurrence):
                continue
            out.append(occurrence)
            if len(out) >= limit:
                return out
    return out


# ---------------------------------------------------------------------------
# which period, and what we say about it
# ---------------------------------------------------------------------------


def reported_period(
    model_version: Mapping[str, Any] | None, expected_date: str
) -> dict[str, Any] | None:
    """The fiscal quarter a report on this date is about.

    The latest period this model carries whose end is on or before the report
    date.  A company reports the quarter it has finished, not the one it is in,
    and the model's own period list is the only place this system holds the
    fiscal calendar -- deriving it from the report date arithmetically would be
    inventing a fiscal year end.
    """

    if not model_version:
        return None
    periods = list(model_version.get("forecast_periods") or [])
    periods += list(model_version.get("realised_periods") or [])
    candidates = [
        dict(period) for period in periods
        if str(period.get("end") or "") and str(period["end"]) <= expected_date
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda period: str(period["end"]))


def forecast_rows(
    model_version: Mapping[str, Any] | None, period_end: str, *,
    limit: int = MAX_FORECAST_ROWS,
) -> list[dict[str, Any]]:
    """Our own line-by-line number for one quarter, each with its refs.

    Every row carries the cell ref, the assumptions behind it and the version
    it was read from, because "our estimate" with no way back to what it rests
    on is the kind of number this whole layer exists to make impossible.
    """

    if not model_version:
        return []
    rows: list[dict[str, Any]] = []
    for line in model_version.get("results") or []:
        for cell in line.get("cells") or []:
            if str((cell.get("period") or {}).get("end")) != str(period_end):
                continue
            rows.append({
                "line_ref": line.get("ref"),
                "label": line.get("label"),
                "unit": line.get("unit") or model_version.get("unit"),
                "kind": cell.get("kind"),
                "status": cell.get("status"),
                "value": cell.get("value"),
                "reason": cell.get("reason"),
                "superseded_by": cell.get("superseded_by"),
                "refs": [
                    ref for ref in [
                        cell.get("ref"), model_version.get("id"),
                    ] if ref
                ] + list(cell.get("assumption_refs") or []),
            })
            break
    return rows[:limit]


def consensus_block(
    company_ref: str,
    period_end: str,
    *,
    reader: Callable[..., Any] | None = None,
    limit: int = MAX_CONSENSUS_ROWS,
) -> dict[str, Any]:
    """What the street is at, or ``available: false`` and why.

    ``reader`` is P11b's ``latest_consensus(company)`` passed by name.  It is a
    parameter and not an import because that authority is being built beside
    this one; when it is absent the honest answer is that this Core holds no
    consensus, and the block says so rather than letting a preview infer a
    street view from four sell-side headlines.  (P14a's reflection contract
    made the same refusal for the same reason.)
    """

    if reader is None:
        return {
            "available": False,
            "reason": "this Core holds no consensus authority (P11b), so there is "
                      "no street number to compare against",
            "rows": [],
            "refs": [],
        }
    try:
        raw = reader(company_ref)
    except Exception as exc:  # noqa: BLE001 - an unreadable street is no street
        return {
            "available": False,
            "reason": f"the consensus reader did not answer: {type(exc).__name__}: {exc}",
            "rows": [], "refs": [],
        }
    if not raw:
        return {
            "available": False,
            "reason": "the consensus authority holds nothing for this company",
            "rows": [], "refs": [],
        }
    rows: list[dict[str, Any]] = []
    for item in (raw.get("estimates") if isinstance(raw, Mapping) else raw) or []:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("period_end") or item.get("period") or "") not in (
            period_end, "", None
        ):
            continue
        refs = [str(ref) for ref in (item.get("refs") or []) if str(ref).strip()]
        if not refs:
            # A consensus figure with no ref is not a consensus figure.
            continue
        rows.append({
            "metric": str(item.get("metric") or item.get("measure") or "unknown"),
            "value": None if item.get("value") is None else str(item["value"]),
            "unit": item.get("unit"),
            "estimates": item.get("estimates"),
            "period_end": str(item.get("period_end") or period_end),
            "refs": refs[:6],
        })
    if not rows:
        return {
            "available": False,
            "reason": "the consensus authority holds nothing for this period with a ref",
            "rows": [], "refs": [],
        }
    rows = rows[:limit]
    return {
        "available": True,
        "reason": "",
        "rows": rows,
        "refs": sorted({ref for row in rows for ref in row["refs"]}),
    }


def guidance_vs_actual(
    profile: Mapping[str, Any] | None, period_end: str | None = None,
) -> dict[str, Any]:
    """Did the company make its own number, for the quarter that just closed.

    Reads P12f's computed event table by name -- ``build_profile`` pairs every
    readable guide with the settled figure that answered it -- and narrows it
    to one period.  A separate, named function because the calibration is not
    the only reader that wants it: the dossier's guidance section and the
    weekly brief both ask the same question of the same table, and a second
    implementation of "did they beat it" is a second answer.

    Nothing is computed here that P12f did not compute: the verdicts are its
    ``beat`` / ``miss`` / ``inline``, and a guide whose unit class does not
    match its actual stays ``unknown`` rather than being coerced.
    """

    if not profile:
        return {
            "available": False,
            "reason": "no guidance profile: this Core holds no readable guidance "
                      "statement for this company",
            "period_end": period_end, "rows": [], "classification": None, "refs": [],
        }
    events = [
        dict(event) for event in (profile.get("events") or [])
        if period_end is None or str(event.get("period") or "") == str(period_end)
    ]
    if not events:
        return {
            "available": False,
            "reason": "the guidance profile has no event for this period",
            "period_end": period_end, "rows": [],
            "classification": (profile.get("classification") or {}).get("style"),
            "refs": [],
        }
    rows: list[dict[str, Any]] = []
    for event in events[:MAX_GUIDANCE_ROWS]:
        guide = event.get("guide") or {}
        actual = event.get("actual") or None
        deviation = event.get("deviation") or {}
        refs = [str(ref) for ref in (guide.get("refs") or []) if str(ref).strip()]
        refs += [str(ref) for ref in ((actual or {}).get("refs") or []) if str(ref).strip()]
        rows.append({
            "measure": event.get("measure"),
            "period": event.get("period"),
            "guide_low": guide.get("low"),
            "guide_high": guide.get("high"),
            "guide_unit": guide.get("unit"),
            "guide_basis": guide.get("guide_basis"),
            "actual": None if actual is None else actual.get("value"),
            "actual_unit": None if actual is None else actual.get("unit"),
            "verdict": deviation.get("verdict"),
            "distance": deviation.get("distance"),
            "reason": deviation.get("reason"),
            "refs": sorted(dict.fromkeys(refs))[:8],
        })
    settled = [row for row in rows if row["verdict"] in ("beat", "miss", "inline")]
    return {
        "available": True,
        "reason": "",
        "period_end": period_end,
        "rows": rows,
        "settled": len(settled),
        "classification": (profile.get("classification") or {}).get("style"),
        "refs": sorted({ref for row in rows for ref in row["refs"]}),
    }


def watch_list(
    *,
    debates: Sequence[Mapping[str, Any]] = (),
    theses: Sequence[Mapping[str, Any]] = (),
    drivers: Sequence[Mapping[str, Any]] = (),
    limit: int = MAX_WATCH_ROWS,
) -> dict[str, Any]:
    """What to watch on the call, and where the list came from.

    P12c's open debates first: an open question the market is arguing about is
    precisely what a print settles or fails to.  When the debate map holds
    nothing for this company -- most of them, for now -- the fallback is the
    thesis's own falsifiers and the model's drivers, which is the same question
    asked from our side of the table.  The source is reported because a preview
    built on the fallback is a thinner preview and the reader should know.
    """

    rows: list[dict[str, Any]] = []
    for debate in debates:
        question = str(debate.get("question") or debate.get("statement") or "").strip()
        if not question:
            continue
        rows.append({
            "source": "open_debate",
            "question": question[:400],
            "refs": [str(ref) for ref in (debate.get("refs") or debate.get("evidence_refs") or [])][:6],
        })
    if rows:
        return {"source": "open_debates", "rows": rows[:limit]}
    for thesis in theses:
        # A ThesisVersion carries its falsifiers as refs rather than as
        # sentences, so the question this asks is the thesis's own mechanism:
        # "what on this call tests the thing we say is driving it".  The
        # falsifier refs travel with the row so a reader can open them.
        mechanism = str(thesis.get("mechanism") or thesis.get("statement") or "").strip()
        if not mechanism:
            continue
        refs = [str(thesis.get("ref") or thesis.get("id") or "")]
        refs += [str(ref) for ref in (thesis.get("falsifier_refs") or ())]
        rows.append({
            "source": "thesis_falsifier",
            "question": f"这次业绩里有什么会检验：{mechanism[:300]}",
            "refs": [ref for ref in refs if ref][:6],
        })
    for driver in drivers:
        label = str(driver.get("label") or driver.get("ref") or "").strip()
        if not label:
            continue
        rows.append({
            "source": "model_driver",
            "question": f"{label}：这一季的实际值与我们的假设差多少",
            "refs": [str(driver.get("ref") or "")],
        })
    return {
        "source": "thesis_falsifiers_and_drivers" if rows else "none",
        "rows": rows[:limit],
    }


def citable_refs(context: Mapping[str, Any]) -> set[str]:
    """Every ref a window's prose is allowed to cite.

    The same rule P14a's ``allowed_refs`` states: a citation of something the
    prompt never showed is a hallucinated citation, and the answer is refused
    whole rather than having the stray ref removed.
    """

    refs: set[str] = set()
    for row in context.get("forecast") or ():
        refs.update(str(ref) for ref in row.get("refs") or ())
    for block in ("consensus", "guidance"):
        refs.update(str(ref) for ref in (context.get(block) or {}).get("refs") or ())
    for row in (context.get("watch") or {}).get("rows") or ():
        refs.update(str(ref) for ref in row.get("refs") or () if str(ref).strip())
    for thesis in context.get("theses") or ():
        for key in ("thesis_version_ref", "id"):
            if thesis.get(key):
                refs.add(str(thesis[key]))
    for row in context.get("claims") or ():
        if row.get("ref"):
            refs.add(str(row["ref"]))
    for row in context.get("reconciliations") or ():
        for key in ("id", "forecast_line_version_ref", "claim_version_ref"):
            if row.get(key):
                refs.add(str(row[key]))
    for ref in context.get("source_refs") or ():
        refs.add(str(ref))
    refs.discard("")
    return refs


def render_rows(rows: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> str:
    """A tab-separated table for a prompt.

    The same choice ``company_model_spec`` and P12f made: the identical content
    as JSON objects costs several times the bytes, and the router reserves
    budget against the size of what it is sent.
    """

    lines = ["\t".join(fields)]
    for row in rows:
        lines.append("\t".join(
            "" if row.get(field) is None else str(row.get(field))
            for field in fields
        ))
    return "\n".join(lines)


__all__ = [
    "CALIBRATION_DEADLINE_DAYS",
    "CALIBRATION_EVENT_KIND",
    "CALIBRATION_KIND",
    "CALIBRATION_PURPOSE",
    "CONFIRMED",
    "CONFIRMED_ONLY_WINDOWS",
    "EarningsSeasonError",
    "EarningsSeasonRefused",
    "EarningsSeasonValidationError",
    "MAX_THESES",
    "PREVIEW_KIND",
    "PREVIEW_PURPOSE",
    "SCHEMA_VERSION",
    "WINDOWS",
    "already_written",
    "citable_refs",
    "consensus_block",
    "date_confidence_of",
    "deliverable_kind_for",
    "forecast_rows",
    "guidance_vs_actual",
    "idempotency_key_for",
    "occurrence_of",
    "occurrence_ref",
    "occurrences_from_calendar",
    "open_occurrences",
    "render_rows",
    "reported_period",
    "watch_list",
    "window_of",
]
