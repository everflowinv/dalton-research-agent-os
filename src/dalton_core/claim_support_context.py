"""2026-09-26b: what the support check reads besides the statement -- whole sentences and the document's facts.

The post-deploy check of batch 2026-09-25d found about half of the support
check's ``not_supported`` verdicts wrong, the cited text all but word for word
the statement: CTSH "organic revenue growth at the high end of our
expectations" filed as "In Q2 2026, Cognizant reported ...", EPAM "we are
particularly seeing underperformance in North America" as "EPAM said ...".
The verifier saw the statement and the cited bytes and nothing else, and its
question counted "who said it" as something the text had to state -- so the
quarter the drafter anchored from the document's date, and the company whose
own earnings call it was, read as facts the text added.  And the backfill cited
the binding's span, which for Claims admitted before the quotes were cut at
sentences is a fixed 1,200-character slice: a statement drawn from the
sentence the slice cut in half was checked against half a sentence.

This module is what every path that asks (admission, backfill, re-review,
recheck) now gives the verifier with each statement:

* ``sentence_bounds`` -- the cited span widened to the whole sentences it
  cuts, never beyond ``MAX_CITED_CHARS`` (the prompt's own bound), so no
  sentence the citation touches is shown in part;
* ``speaker_at`` -- who is speaking at the span, from a transcript's own
  speaker label (``发言人Ravi Kumar：``), never inferred from prose;
* ``document_facts`` -- the title, date and publishing house the Core already
  holds for the document (``document_provenance_records``; a sales note's
  subject, send date and sending house off the raw ``get_note`` header, the
  same bytes ``sales_note_broker`` reads), plus the period the statement is
  filed under.  Every fact is optional; nothing is guessed.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from typing import Any

#: The sentence ends the extraction quotes are cut at
#: (``document_extraction._SENTENCE_END_RE``): a terminator followed by
#: whitespace, a closing quote or bracket, or a line break -- and one more:
#: AlphaEngine's transcripts run sentences together ("ramp up.That's why"), so
#: a terminator after a lower-case letter or digit and before a capital ends a
#: sentence too ("U.S.A" does not: its full stops follow capitals).
_SENTENCE_END_RE = re.compile(
    r"(?:[.!?;](?=[\s\"'”’)\]]|$)|(?<=[a-z0-9])[.!?](?=[A-Z])|[。！？；]|\n)[\"'”’)\]]*\s*"
)
#: AlphaEngine's transcripts label every turn ``发言人<name>：``.
_SPEAKER_RE = re.compile(r"发言人\s*([^：:\n]{1,80}?)\s*[：:]")
_SPEAKER_LOOKBACK = 200_000
MAX_FACT_CHARS = 200
FACT_KEYS = ("title", "date", "house", "period", "speaker")


def sentence_bounds(text: str, start: int, end: int, *, max_chars: int) -> tuple[int, int]:
    """``[start, end)`` widened to the whole sentences it cuts, within ``max_chars``.

    The start moves back to the sentence end before it, the end forward to the
    sentence end after it.  A side whose boundary is not within reach stays
    where it was (unless the reach is the document's own edge): a span is
    never widened into another half sentence.  Surrounding whitespace is
    trimmed.  Anything but a valid span is returned unchanged.
    """

    if not isinstance(text, str) or not (0 <= start < end <= len(text)):
        return start, end
    budget = int(max_chars) - (end - start)
    if budget <= 0:
        return start, end
    new_start = start
    low = max(0, start - budget)
    last = None
    for match in _SENTENCE_END_RE.finditer(text, low, start):
        last = match.end()
    if last is not None:
        new_start = last
    elif low == 0:
        new_start = 0
    high = min(len(text), end + budget - (start - new_start))
    new_end = end
    found = None
    # Where the span's own text ends: trailing whitespace is not the cut.
    last_char = end
    while last_char > start and text[last_char - 1].isspace():
        last_char -= 1
    for match in _SENTENCE_END_RE.finditer(text, max(new_start, last_char - 1), high):
        found = match.end()
        break
    if found is not None:
        new_end = max(end, found)
    elif high == len(text):
        new_end = high
    while new_start < start and text[new_start].isspace():
        new_start += 1
    while new_end > end and text[new_end - 1].isspace():
        new_end -= 1
    return new_start, new_end


def speaker_at(text: str, position: int) -> str | None:
    """The transcript speaker whose turn ``position`` is in, or None."""

    if not isinstance(text, str) or not 0 <= position <= len(text):
        return None
    floor = max(0, position - _SPEAKER_LOOKBACK)
    # A label that starts at ``position`` is the one in effect there.
    index = text.rfind("发言人", floor, position + len("发言人"))
    while index != -1:
        match = _SPEAKER_RE.match(text, index)
        if match is not None:
            name = match.group(1).strip()
            return name[:MAX_FACT_CHARS] or None
        index = text.rfind("发言人", floor, index)
    return None


def cited_passage(text: str, start: int, end: int, *, max_chars: int) -> dict[str, Any]:
    """The whole-sentence passage around ``[start, end)`` and its speaker."""

    new_start, new_end = sentence_bounds(text, start, end, max_chars=max_chars)
    return {"cited_text": text[new_start:new_end], "start": new_start, "end": new_end,
            "speaker": speaker_at(text, new_start)}


def _day(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) < 10 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value[:10]):
        return None
    return value[:10]


def _fact(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value[:MAX_FACT_CHARS] or None


def provenance_facts(connection: sqlite3.Connection, document_ref: Any) -> dict[str, str]:
    """Title, date and house from the document's provenance row, when it has one."""

    if not isinstance(document_ref, str) or not document_ref:
        return {}
    try:
        row = connection.execute(
            "SELECT broker, title, published_at FROM document_provenance_records WHERE document_ref=?",
            (document_ref,),
        ).fetchone()
    except sqlite3.Error:
        return {}
    if row is None:
        return {}
    facts = {"title": _fact(row[1]), "date": _day(row[2]), "house": _fact(row[0])}
    return {key: value for key, value in facts.items() if value}


def sales_note_facts(connection: sqlite3.Connection, spool: Any,
                     source_envelope_ref: Any) -> dict[str, str]:
    """A sales note's subject, send date and house, off its raw ``get_note`` header.

    The envelope names the raw artefact, the artefact names its content hash
    and the spool bytes must hash to it -- the chain ``sales_note_broker``
    reads the house through.  Anything missing is simply no facts.
    """

    from .sales_note_broker import broker_for_sender_domain, note_header

    if spool is None or not isinstance(source_envelope_ref, str) or not source_envelope_ref:
        return {}
    try:
        row = connection.execute(
            "SELECT a.artifact_content_hash FROM connector_source_envelopes e "
            "JOIN observability_artifact_versions_v2 a "
            "  ON a.version_id=json_extract(e.record_json,'$.raw_artifact_version_ref') "
            "WHERE e.source_envelope_id=?", (source_envelope_ref,),
        ).fetchone()
    except sqlite3.Error:
        return {}
    if row is None or not isinstance(row[0], str):
        return {}
    try:
        raw = spool.read_object(row[0])
    except Exception:  # noqa: BLE001 - a pruned artefact is no facts, not a failure
        return {}
    if hashlib.sha256(raw).hexdigest() != row[0]:
        return {}
    header = note_header(raw)
    if header is None:
        return {}
    facts = {"title": _fact(header.get("subject")), "date": _day(header.get("sent_at")),
             "house": broker_for_sender_domain(header.get("sender_domain"))}
    return {key: value for key, value in facts.items() if value}


def document_facts(
    connection: sqlite3.Connection | None = None,
    *,
    document_ref: Any = None,
    source_envelope_ref: Any = None,
    spool: Any = None,
    document_date: Any = None,
    period: Any = None,
    speaker: Any = None,
) -> dict[str, str]:
    """What the verifier is told about the document a statement cites.

    A date the caller holds (the extraction context's manifest date) comes
    first, then the provenance row, then a sales note's own header.  The keys
    are exactly ``FACT_KEYS``; a fact nobody holds is absent.
    """

    facts: dict[str, str] = {}
    if _day(document_date):
        facts["date"] = _day(document_date)
    if connection is not None:
        for source in (provenance_facts(connection, document_ref),
                       sales_note_facts(connection, spool, source_envelope_ref)):
            for key, value in source.items():
                facts.setdefault(key, value)
    if _fact(period):
        facts["period"] = _fact(period)
    if _fact(speaker):
        facts["speaker"] = _fact(speaker)
    return {key: facts[key] for key in FACT_KEYS if key in facts}


def facts_digest(facts: Mapping[str, Any] | None) -> str:
    return hashlib.sha256(json.dumps(dict(facts or {}), sort_keys=True, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


__all__ = [
    "FACT_KEYS",
    "cited_passage",
    "document_facts",
    "facts_digest",
    "provenance_facts",
    "sales_note_facts",
    "sentence_bounds",
    "speaker_at",
]
