"""W4: once a month, forget what we already decided and ask again from zero.

Chem's coverage agent had this idea and never ran it once -- the
``dalton-coverage-zero-base`` cron fired zero times in the whole archive, and
the three ``zero_base_review`` tasks in its list were never picked up.  The
idea survived the review because it is the right correction for the failure
mode we are heading into: a system whose theses accumulate, whose debates
never close, and whose judgement lane answers ``no_change`` because nothing
happened rather than because nothing should change.

So the review asks four questions, and only these four, from a zero position:

1. **Would we form a view on this company at all if we were seeing it for the
   first time today?**  ``yes`` / ``no`` / ``unclear``, with a because.  This
   is the question the accumulated file cannot answer, because the file is
   the accumulation.
2. **Which lines of the current thesis would be written differently?**  Each
   one names the thesis version it is rewriting and carries a decision word
   from the Playbook's frozen vocabulary.
3. **Which debates are no longer material?**  A debate map that only grows is
   a list, not a map.
4. **What is the next verification point, and on what date?**  A review that
   ends without a dated next look is a review that will be repeated from
   scratch next month.

Three properties are the design.

**It proposes and never rewrites.**  The output is a deliverable-shaped record
in this module's own chain, plus -- where an answer to question 2 implies a
change -- ``ThesisRevisionCandidate`` rows that go to the same human decision
loop, the same decision ledger and the same thesis chain that ADR-0007 built
for the judgement lane.  Nothing here can move a pointer on a thesis, a
dossier or a debate map.

**The cadence is a fact about the ledger, not a timer.**  A review is owed
when this company has no review in the prior 30 days, or when an earnings
calibration has been published that no review has been written against.  Both
are one read, so a writer restart in the middle of a month does not produce a
second review, and the ``inputs_hash`` rule underneath means that even a lane
that fired ten times pays once.

**Nothing is repaired.**  An answer with an extra key, a citation to a ref
that was not in the prompt, a rewrite naming a thesis this company does not
have, or a verification date in the past is refused as a whole.  This is the
judgement lane's rule and it is here for the judgement lane's reason: a review
that has to be cleaned up before it can be read is not a review.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .cockpit_model import (
    CockpitModelError,
    lane_status_for,
    register_purpose,
    unwrap_json_object,
)
from .research_playbook import DECISION_VOCABULARY
from .store import canonical_json, content_hash

SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("zero_base_review_schema.sql")

#: Registered at import, like every other purpose: a purpose registered after
#: the call that uses it is not registered.
PURPOSE = register_purpose("zero_base_review")
VERIFIER_PURPOSE = register_purpose("zero_base_review_verifier")

#: The scope a review is published under.  ``deliverable``: a dated document
#: about one company, produced on a cadence and read by a person -- the same
#: class of thing as the weekly reflection, and like it, asserting nothing
#: about any company that a Claim would have to back.
WRITE_SCOPE = "deliverable"
#: The scope and the checkpoint a *candidate* needs.  Both, or none: a mission
#: that grants the write without the checkpoint would let a proposal be
#: recorded with nobody obliged to answer it.  This is the judgement lane's
#: rule, reused verbatim rather than restated.
CANDIDATE_SCOPE = "thesis_revision_candidate"
CHECKPOINT_KIND = "thesis_revision_candidate"

#: Closed.  A third trigger would be a cadence decision, and cadence decisions
#: are policy rather than code.
TRIGGERS: tuple[str, ...] = ("monthly", "earnings_calibration")

#: The first question's three answers.  ``unclear`` is here on purpose: a
#: review forced to choose between yes and no on a company it cannot read is a
#: review that will answer yes.
FORM_A_VIEW: tuple[str, ...] = ("yes", "no", "unclear")

#: Which decision words a *rewrite* may carry.  ``NO_CHANGE`` is not a
#: rewrite, and ``NEW_THESIS`` is a coverage admission rather than a revision
#: (ADR-0007) -- a candidate carrying it would be refused at the moment a
#: person tried to accept it, which is the worst possible time to find out.
REWRITE_DECISIONS: tuple[str, ...] = tuple(
    word for word in DECISION_VOCABULARY
    if word not in ("NO_CHANGE", "NEW_THESIS")
)

MAX_REWRITTEN_LINES = 4
MAX_STALE_DEBATES = 4
MAX_BECAUSE_CHARS = 1200
MAX_LINE_CHARS = 1200
MAX_REFS_PER_ITEM = 8
MAX_CITATIONS = 16
MAX_THESES_IN_PROMPT = 8
MAX_DEBATES_IN_PROMPT = 12
MAX_EVENTS_IN_PROMPT = 12
MAX_CLAIMS_IN_PROMPT = 10
MAX_PROMPT_CHARS = 26_000

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")

#: The exact key set an answer may have.  Checked as a set, not as a
#: superset: an extra key is a different contract.
OUTPUT_KEYS: frozenset[str] = frozenset({
    "form_a_view", "because", "rewritten_lines", "stale_debates",
    "next_verification", "citations",
})
_REWRITE_KEYS: frozenset[str] = frozenset({
    "thesis_ref", "decision", "line", "because", "refs",
})
_STALE_KEYS: frozenset[str] = frozenset({"debate_ref", "because"})
_VERIFICATION_KEYS: frozenset[str] = frozenset({"what", "date", "because"})


class ZeroBaseReviewError(RuntimeError):
    """Base error for the zero-base review."""


class ZeroBaseReviewValidationError(ZeroBaseReviewError, ValueError):
    """A model output or a request does not satisfy the closed contract."""


class ZeroBaseReviewConflict(ZeroBaseReviewError):
    """A request conflicts with the append-only ledger."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ZeroBaseReviewValidationError(f"{name} must be non-empty text")
    text = value.strip()
    if len(text) > maximum:
        raise ZeroBaseReviewValidationError(f"{name} is longer than {maximum} characters")
    return text


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def review_ref_for(mission_ref: str, company_ref: str) -> str:
    """One chain per company per mission."""

    return "zero-base-review:" + content_hash(
        {"mission": mission_ref, "company": company_ref}
    )[:32]


def month_label(moment: datetime) -> str:
    """The month a review belongs to, in the owner's local calendar.

    Local rather than UTC for the reason the weekly reflection uses the
    owner's Monday: a review that flipped into the next month at 20:00 on the
    30th would be a review of a month the owner has not finished living in.
    """

    return f"{moment.year:04d}-{moment.month:02d}"


# ---------------------------------------------------------------------------
# when a review is owed
# ---------------------------------------------------------------------------


def latest_calibration(
    connection: sqlite3.Connection, company_ref: str
) -> dict[str, Any] | None:
    """The newest earnings calibration published for this company, or nothing."""

    if not _table_exists(connection, "mission_deliverable_versions"):
        return None
    row = connection.execute(
        "SELECT version_id, deliverable_ref, version_number, created_at "
        "FROM mission_deliverable_versions "
        "WHERE kind='earnings_calibration' AND subject_ref=? "
        "ORDER BY created_at DESC, version_id DESC LIMIT 1",
        (company_ref,),
    ).fetchone()
    if row is None:
        return None
    return {
        "version_ref": row["version_id"],
        "deliverable_ref": row["deliverable_ref"],
        "version": int(row["version_number"]),
        "created_at": row["created_at"],
    }


def _reviewed_periods(
    connection: sqlite3.Connection, review_ref: str
) -> set[tuple[str, str]]:
    if not _table_exists(connection, "zero_base_review_versions"):
        return set()
    return {
        (str(row["trigger"]), str(row["period_label"]))
        for row in connection.execute(
            "SELECT trigger, period_label FROM zero_base_review_versions "
            "WHERE review_ref=?", (review_ref,),
        ).fetchall()
    }


def _latest_review_at(connection: sqlite3.Connection, review_ref: str) -> datetime | None:
    if not _table_exists(connection, "zero_base_review_versions"):
        return None
    row = connection.execute(
        "SELECT created_at FROM zero_base_review_versions WHERE review_ref=? "
        "ORDER BY created_at DESC, version_id DESC LIMIT 1", (review_ref,),
    ).fetchone()
    if row is None:
        return None
    return datetime.fromisoformat(str(row["created_at"]))


def review_state(
    connection: sqlite3.Connection,
    *,
    mission_ref: str,
    company_refs: Sequence[str],
    now: datetime,
) -> list[dict[str, Any]]:
    """For every covered company, whether a review is owed and why.

    Read-only, so the lane tick can ask it without holding anything that can
    write.  The earnings trigger is checked first: a print is the moment the
    accumulated story is most likely to be wrong, and it restarts the 30-day
    clock once the earnings-triggered review is recorded.
    """

    month = month_label(now)
    state: list[dict[str, Any]] = []
    for company_ref in company_refs:
        review_ref = review_ref_for(mission_ref, company_ref)
        seen = _reviewed_periods(connection, review_ref)
        calibration = latest_calibration(connection, company_ref)
        if calibration is not None and (
            "earnings_calibration", calibration["version_ref"]
        ) not in seen:
            state.append({
                "company_ref": company_ref, "review_ref": review_ref, "due": True,
                "trigger": "earnings_calibration",
                "period_label": calibration["version_ref"],
                "calibration": calibration,
                "reason": "这家公司出了一版新的财报对账，还没有从零重问过",
            })
            continue
        latest_review = _latest_review_at(connection, review_ref)
        if latest_review is None or now - latest_review >= timedelta(days=30):
            state.append({
                "company_ref": company_ref, "review_ref": review_ref, "due": True,
                "trigger": "monthly", "period_label": month, "calibration": calibration,
                "reason": "距这家公司上一版零基复盘已满 30 天",
            })
            continue
        state.append({
            "company_ref": company_ref, "review_ref": review_ref, "due": False,
            "trigger": "monthly", "period_label": month, "calibration": calibration,
            "reason": "距上一版复盘不足 30 天，且没有新的财报对账",
        })
    return state


def due_reviews(
    connection: sqlite3.Connection,
    *,
    mission_ref: str,
    company_refs: Sequence[str],
    now: datetime,
) -> list[dict[str, Any]]:
    return [row for row in review_state(
        connection, mission_ref=mission_ref, company_refs=company_refs, now=now
    ) if row["due"]]


# ---------------------------------------------------------------------------
# the table the review reads
# ---------------------------------------------------------------------------


def company_debates(
    connection: sqlite3.Connection, company_ref: str, *, limit: int = MAX_DEBATES_IN_PROMPT
) -> list[dict[str, Any]]:
    """The live debates on the company's current map, newest map only."""

    if not _table_exists(connection, "debate_map_versions"):
        return []
    row = connection.execute(
        "SELECT record_json FROM debate_map_versions WHERE subject_ref=? "
        "ORDER BY version_number DESC LIMIT 1", (company_ref,),
    ).fetchone()
    if row is None:
        return []
    try:
        record = json.loads(row["record_json"])
    except ValueError:  # pragma: no cover - a row nothing could have written
        return []
    debates = []
    for debate in (record.get("debates") or ())[:limit]:
        if not isinstance(debate, Mapping):
            continue
        ours = debate.get("our_position") or {}
        debates.append({
            "ref": str(debate.get("debate_ref") or ""),
            "question": str(debate.get("question") or "")[:400],
            "status": debate.get("status"),
            "our_side": ours.get("side") if isinstance(ours, Mapping) else None,
            "our_state": ours.get("state") if isinstance(ours, Mapping) else None,
        })
    return [debate for debate in debates if debate["ref"]]


def recent_events(
    connection: sqlite3.Connection, company_ref: str, *, limit: int = MAX_EVENTS_IN_PROMPT
) -> list[dict[str, Any]]:
    if not _table_exists(connection, "research_events"):
        return []
    rows = connection.execute(
        "SELECT event_id, kind, evidence_tier, occurred_at FROM research_events "
        "WHERE company_ref=? ORDER BY occurred_at DESC, event_id DESC LIMIT ?",
        (company_ref, max(1, int(limit))),
    ).fetchall()
    return [
        {"ref": row["event_id"], "kind": row["kind"],
         "evidence_tier": row["evidence_tier"], "occurred_at": row["occurred_at"]}
        for row in rows
    ]


def build_context(
    connection: sqlite3.Connection,
    *,
    mission: Mapping[str, Any],
    company_ref: str,
    trigger: str,
    period_label: str,
    now: datetime,
    calibration: Mapping[str, Any] | None = None,
    prior_review: Mapping[str, Any] | None = None,
    outcome_counts: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Everything the one call is allowed to look at, and nothing else.

    A table rather than a narrative, for the judgement lane's reason: every
    line of it carries a ref the answer may cite, and a narrative would let
    the answer cite the narrative.
    """

    from .event_judgement import company_theses, recent_claims

    if trigger not in TRIGGERS:
        raise ZeroBaseReviewValidationError(f"trigger is one of {list(TRIGGERS)}")
    theses = company_theses(connection, company_ref)[:MAX_THESES_IN_PROMPT]
    debates = company_debates(connection, company_ref)
    events = recent_events(connection, company_ref)
    claims = recent_claims(connection, company_ref, limit=MAX_CLAIMS_IN_PROMPT)
    context = {
        "schema_version": SCHEMA_VERSION,
        "company_ref": company_ref,
        "mission_ref": _text(mission.get("mission_ref"), "mission.mission_ref"),
        "mission_version_ref": _text(mission.get("id"), "mission.id"),
        "trigger": trigger,
        "period_label": _text(period_label, "period_label", maximum=256),
        "as_of": now.date().isoformat(),
        "theses": theses,
        "debates": debates,
        "events": events,
        "claims": claims,
        "calibration": dict(calibration) if calibration else None,
        "prior_review": None if prior_review is None else {
            "ref": prior_review.get("id"),
            "version": prior_review.get("version"),
            "created_at": prior_review.get("created_at"),
            "form_a_view": prior_review.get("form_a_view"),
            "next_verification": prior_review.get("next_verification"),
        },
        "outcome_counts": dict(outcome_counts or {}),
    }
    context["allowed_refs"] = allowed_refs(context)
    context["inputs_hash"] = inputs_hash(context)
    return context


def allowed_refs(context: Mapping[str, Any]) -> list[str]:
    """Every ref the answer may cite.  A citation to anything else is refused."""

    refs: list[str] = []
    for thesis in context.get("theses") or ():
        refs.append(str(thesis["ref"]))
    for debate in context.get("debates") or ():
        refs.append(str(debate["ref"]))
    for event in context.get("events") or ():
        refs.append(str(event["ref"]))
    for claim in context.get("claims") or ():
        ref = claim.get("ref") or claim.get("claim_version_ref")
        if ref:
            refs.append(str(ref))
    calibration = context.get("calibration")
    if calibration:
        refs.append(str(calibration["version_ref"]))
    prior = context.get("prior_review")
    if prior and prior.get("ref"):
        refs.append(str(prior["ref"]))
    return sorted(dict.fromkeys(refs))


def inputs_hash(context: Mapping[str, Any]) -> str:
    """What makes two readings of one company the same reading.

    Everything the prompt was built from, and nothing derived from the answer:
    hashing the answer would make the duplicate rule depend on its own output,
    which is the shape that never fires.
    """

    return content_hash({
        "schema_version": SCHEMA_VERSION,
        "company_ref": context["company_ref"],
        "trigger": context["trigger"],
        "period_label": context["period_label"],
        "theses": context.get("theses") or [],
        "debates": context.get("debates") or [],
        "events": context.get("events") or [],
        "claims": context.get("claims") or [],
        "calibration": context.get("calibration"),
        "outcome_counts": context.get("outcome_counts") or {},
    })


# ---------------------------------------------------------------------------
# the prompt
# ---------------------------------------------------------------------------

QUESTIONS: tuple[str, ...] = (
    "If you were seeing this company for the first time today, would you form a "
    "view on it at all?",
    "Which lines of the current thesis would you write differently?",
    "Which of the open debates are no longer material?",
    "What is the next verification point, and on what date?",
)


def _rows(title: str, rows: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> list[str]:
    if not rows:
        return [f"{title}: (none)"]
    lines = [f"{title}:"]
    for row in rows:
        parts = [f"{field}={row.get(field)}" for field in fields if row.get(field) is not None]
        lines.append("  " + "; ".join(parts))
    return lines


def build_review_prompt(context: Mapping[str, Any]) -> str:
    """The four questions, the table they are asked against, and the schema."""

    lines = [
        "You are the covering analyst. Forget, for the length of this answer, that this",
        "fund already holds a view on this company. Nothing has happened that requires a",
        "decision: this is the monthly zero-base review, and the only thing being asked is",
        "whether the accumulated file would be written the same way from a standing start.",
        "",
        f"Company: {context['company_ref']}",
        f"As of: {context['as_of']}   Trigger: {context['trigger']}   Period: {context['period_label']}",
        "",
        "Answer these four and nothing else:",
    ]
    lines += [f"  {index}. {question}" for index, question in enumerate(QUESTIONS, start=1)]
    lines += [
        "",
        "Deciding that we would form the same view is a real answer and is often the right",
        "one -- but it must be argued from what is in the table below, not from the fact",
        "that we already hold it. 'No debate has closed' is not an answer to question 3;",
        "a debate that both sides now agree on, or that the numbers have settled, is.",
        "",
    ]
    lines += _rows("Current theses", context.get("theses") or (),
                   ("ref", "statement", "confidence", "mechanism"))
    lines += [""]
    lines += _rows("Open debates", context.get("debates") or (),
                   ("ref", "question", "status", "our_side"))
    lines += [""]
    lines += _rows("Recent events", context.get("events") or (),
                   ("ref", "kind", "evidence_tier", "occurred_at"))
    lines += [""]
    lines += _rows("Recent claims", context.get("claims") or (),
                   ("ref", "statement", "aspect"))
    calibration = context.get("calibration")
    if calibration:
        lines += ["", f"Latest earnings calibration: {calibration['version_ref']} "
                      f"({calibration.get('created_at')})"]
    prior = context.get("prior_review")
    if prior:
        lines += ["", f"Last zero-base review: {prior.get('ref')} v{prior.get('version')} "
                      f"said form_a_view={prior.get('form_a_view')}; its next verification "
                      f"point was {(prior.get('next_verification') or {}).get('what')} "
                      f"on {(prior.get('next_verification') or {}).get('date')}"]
    counts = context.get("outcome_counts") or {}
    if counts:
        lines += ["", "How our past judgements on this company turned out (derived, no model): "
                      + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())
                                  if value)]
    lines += [
        "",
        "Every ref you cite must be one of these, exactly as written:",
        "  " + (", ".join(context.get("allowed_refs") or ()) or "(none)"),
        "",
        "Reply with one JSON object and nothing else:",
        "{",
        '  "form_a_view": "yes" | "no" | "unclear",',
        '  "because": "<why, in at most a few sentences>",',
        '  "rewritten_lines": [{"thesis_ref": "<one of the thesis refs above>",',
        f'    "decision": one of {list(REWRITE_DECISIONS)},',
        '    "line": "<how that thesis would read if written today>",',
        '    "because": "<why>", "refs": ["<refs>"]}],',
        '  "stale_debates": [{"debate_ref": "<one of the debate refs above>",',
        '    "because": "<why it no longer matters>"}],',
        '  "next_verification": {"what": "<the observable>", "date": "YYYY-MM-DD",',
        '    "because": "<why that is the thing to watch>"},',
        '  "citations": ["<refs you relied on>"]',
        "}",
        "",
        f"At most {MAX_REWRITTEN_LINES} rewritten lines and {MAX_STALE_DEBATES} stale",
        "debates; empty lists are correct answers. The verification date must be on or",
        "after the as-of date. Do not add keys and do not cite a ref that is not listed.",
    ]
    prompt = "\n".join(lines)
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ZeroBaseReviewValidationError(
            f"the review prompt is {len(prompt)} characters, over {MAX_PROMPT_CHARS}"
        )
    return prompt


# ---------------------------------------------------------------------------
# the answer
# ---------------------------------------------------------------------------


def _refs(value: Any, name: str, permitted: set[str], *, maximum: int) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ZeroBaseReviewValidationError(f"{name} must be a list of refs")
    refs = [_text(item, f"{name}[]") for item in value]
    if len(refs) > maximum:
        raise ZeroBaseReviewValidationError(f"{name} may name at most {maximum} refs")
    for ref in refs:
        if ref not in permitted:
            raise ZeroBaseReviewValidationError(
                f"{name} cites {ref!r}, which was not in the prompt"
            )
    return list(dict.fromkeys(refs))


def validate_review_output(
    value: Any, context: Mapping[str, Any]
) -> dict[str, Any]:
    """The closed contract.  Anything outside it is refused whole."""

    if not isinstance(value, Mapping):
        raise ZeroBaseReviewValidationError("the review must be one JSON object")
    if set(value) != OUTPUT_KEYS:
        raise ZeroBaseReviewValidationError(
            "a review is exactly " + ", ".join(sorted(OUTPUT_KEYS))
        )
    form = value["form_a_view"]
    if form not in FORM_A_VIEW:
        raise ZeroBaseReviewValidationError(f"form_a_view is one of {list(FORM_A_VIEW)}")
    permitted = set(context.get("allowed_refs") or ())
    thesis_refs = {str(thesis["ref"]) for thesis in context.get("theses") or ()}
    debate_refs = {str(debate["ref"]) for debate in context.get("debates") or ()}

    rewritten = value["rewritten_lines"]
    if not isinstance(rewritten, Sequence) or isinstance(rewritten, (str, bytes)):
        raise ZeroBaseReviewValidationError("rewritten_lines must be a list")
    if len(rewritten) > MAX_REWRITTEN_LINES:
        raise ZeroBaseReviewValidationError(
            f"at most {MAX_REWRITTEN_LINES} rewritten lines"
        )
    lines: list[dict[str, Any]] = []
    seen_theses: set[str] = set()
    for item in rewritten:
        if not isinstance(item, Mapping) or set(item) != _REWRITE_KEYS:
            raise ZeroBaseReviewValidationError(
                "a rewritten line is exactly " + ", ".join(sorted(_REWRITE_KEYS))
            )
        thesis_ref = _text(item["thesis_ref"], "rewritten_lines[].thesis_ref")
        if thesis_ref not in thesis_refs:
            raise ZeroBaseReviewValidationError(
                f"rewritten_lines names {thesis_ref!r}, which is not a thesis this "
                "company has"
            )
        if thesis_ref in seen_theses:
            # Two rewrites of one thesis version would become two candidates
            # against the same version, and the second could never be accepted
            # after the first was. Refusing here is cheaper than finding out
            # at the human checkpoint.
            raise ZeroBaseReviewValidationError(
                f"rewritten_lines names {thesis_ref!r} twice; one rewrite per thesis"
            )
        seen_theses.add(thesis_ref)
        decision = item["decision"]
        if decision not in REWRITE_DECISIONS:
            raise ZeroBaseReviewValidationError(
                f"a rewrite carries one of {list(REWRITE_DECISIONS)}"
            )
        refs = _refs(item["refs"], "rewritten_lines[].refs", permitted,
                     maximum=MAX_REFS_PER_ITEM)
        if not refs:
            raise ZeroBaseReviewValidationError(
                "a rewritten line must name the evidence that occasioned it (ADR-0007)"
            )
        lines.append({
            "thesis_ref": thesis_ref,
            "decision": decision,
            "line": _text(item["line"], "rewritten_lines[].line", maximum=MAX_LINE_CHARS),
            "because": _text(item["because"], "rewritten_lines[].because",
                             maximum=MAX_BECAUSE_CHARS),
            "refs": refs,
        })

    stale = value["stale_debates"]
    if not isinstance(stale, Sequence) or isinstance(stale, (str, bytes)):
        raise ZeroBaseReviewValidationError("stale_debates must be a list")
    if len(stale) > MAX_STALE_DEBATES:
        raise ZeroBaseReviewValidationError(f"at most {MAX_STALE_DEBATES} stale debates")
    debates: list[dict[str, Any]] = []
    for item in stale:
        if not isinstance(item, Mapping) or set(item) != _STALE_KEYS:
            raise ZeroBaseReviewValidationError(
                "a stale debate is exactly " + ", ".join(sorted(_STALE_KEYS))
            )
        debate_ref = _text(item["debate_ref"], "stale_debates[].debate_ref")
        if debate_ref not in debate_refs:
            raise ZeroBaseReviewValidationError(
                f"stale_debates names {debate_ref!r}, which is not a debate on this "
                "company's map"
            )
        debates.append({
            "debate_ref": debate_ref,
            "because": _text(item["because"], "stale_debates[].because",
                             maximum=MAX_BECAUSE_CHARS),
        })

    verification = value["next_verification"]
    if not isinstance(verification, Mapping) or set(verification) != _VERIFICATION_KEYS:
        raise ZeroBaseReviewValidationError(
            "next_verification is exactly " + ", ".join(sorted(_VERIFICATION_KEYS))
        )
    when = _text(verification["date"], "next_verification.date", maximum=16)
    if not _DATE_RE.fullmatch(when):
        raise ZeroBaseReviewValidationError("next_verification.date is YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(when)
    except ValueError as exc:
        raise ZeroBaseReviewValidationError("next_verification.date is not a date") from exc
    as_of = date.fromisoformat(str(context["as_of"]))
    if parsed < as_of:
        raise ZeroBaseReviewValidationError(
            "the next verification point is in the past; a date behind the review "
            "is not a next look"
        )
    return {
        "form_a_view": form,
        "because": _text(value["because"], "because", maximum=MAX_BECAUSE_CHARS),
        "rewritten_lines": lines,
        "stale_debates": debates,
        "next_verification": {
            "what": _text(verification["what"], "next_verification.what", maximum=500),
            "date": when,
            "because": _text(verification["because"], "next_verification.because",
                             maximum=MAX_BECAUSE_CHARS),
        },
        "citations": _refs(value["citations"], "citations", permitted,
                           maximum=MAX_CITATIONS),
    }


def _provenance(call: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "work_order_ref": call.get("work_order_ref"),
        "invocation_ref": call.get("invocation_ref"),
        "route_decision_ref": call.get("route_decision_ref"),
        "cost_micros": call.get("cost_micros"),
    }


def review(
    context: Mapping[str, Any],
    *,
    model: Any,
    mission: Mapping[str, Any],
    request_id: str,
) -> dict[str, Any]:
    """One bounded call, believed only after it satisfies the closed schema."""

    prompt = build_review_prompt(context)
    try:
        call = model.call(
            purpose=PURPOSE, request_id=request_id, prompt=prompt, mission=mission
        )
    except CockpitModelError as exc:
        return {"status": "refused", "reason": f"the model call did not succeed: {exc}",
                "lane_status": lane_status_for(exc, "refused"),
                "prompt_chars": len(prompt), "model": None}
    provenance = _provenance(call)
    try:
        validated = validate_review_output(unwrap_json_object(call["text"]), context)
    except ZeroBaseReviewValidationError as exc:
        return {"status": "refused", "reason": str(exc),
                "prompt_chars": len(prompt), "model": provenance}
    return {"status": "reviewed", "prompt_chars": len(prompt), "model": provenance,
            **validated}


def build_verifier_prompt(context: Mapping[str, Any], answered: Mapping[str, Any]) -> str:
    archived = [ref for ref in context.get("allowed_refs") or () if str(ref).startswith(
        ("thesis", "debate", "claim")
    )]
    return "\n".join([
        "You are an independent verifier. Check all four zero-base answers.",
        "Each answer must be grounded in archived thesis, debate, or claim refs, and the",
        "next verification point must contain a real date on or after the as-of date.",
        f"As of: {context['as_of']}",
        "Archived refs: " + ", ".join(archived),
        "Review: " + canonical_json({key: answered[key] for key in OUTPUT_KEYS}),
        'Return raw JSON only: {"verdict":"pass|reject","findings":["<reason>"]}',
        "A pass has no findings; a reject has at least one.",
    ])[:MAX_PROMPT_CHARS]


def verify_review(
    context: Mapping[str, Any], answered: Mapping[str, Any], *, model: Any,
    mission: Mapping[str, Any], request_id: str,
    family_resolver: Callable[[str | None], str | None],
) -> dict[str, Any]:
    producer_family = family_resolver((answered.get("model") or {}).get("route_decision_ref"))
    independence = {"producer_family": producer_family, "verifier_family": None,
                    "predicate": "model_family_ne"}
    if producer_family is None:
        return {"status": "refused", "reason": "the producer model family could not be resolved",
                "independence": independence, "model": None}
    prompt = build_verifier_prompt(context, answered)
    try:
        call = model.call(purpose=VERIFIER_PURPOSE, request_id=request_id,
                          prompt=prompt, mission=mission)
    except CockpitModelError as exc:
        return {"status": "refused", "reason": f"the verifier call did not succeed: {exc}",
                "lane_status": lane_status_for(exc, "refused"), "model": None,
                "independence": independence}
    provenance = _provenance(call)
    verifier_family = family_resolver(provenance.get("route_decision_ref"))
    independence["verifier_family"] = verifier_family
    if verifier_family is None or verifier_family == producer_family:
        return {"status": "refused", "reason": "model_family_not_independent",
                "independence": independence, "model": provenance}
    try:
        payload = unwrap_json_object(call["text"])
        if not isinstance(payload, Mapping) or set(payload) != {"verdict", "findings"}:
            raise ZeroBaseReviewValidationError("the verifier output is exactly verdict and findings")
        findings = payload["findings"]
        if payload["verdict"] not in ("pass", "reject") or not isinstance(findings, list):
            raise ZeroBaseReviewValidationError("the verifier verdict is pass or reject and findings is a list")
        if (payload["verdict"] == "pass") == bool(findings):
            raise ZeroBaseReviewValidationError("a pass has no findings and a reject has at least one")
        findings = [_text(row, "verifier finding", maximum=500) for row in findings]
    except ZeroBaseReviewValidationError as exc:
        return {"status": "refused", "reason": str(exc), "independence": independence,
                "model": provenance}
    if payload["verdict"] != "pass":
        return {"status": "refused", "reason": "; ".join(findings),
                "independence": independence, "model": provenance, "findings": findings}
    return {"status": "verified", "independence": independence, "model": provenance,
            "findings": []}


# ---------------------------------------------------------------------------
# the record a person reads
# ---------------------------------------------------------------------------

NARRATIVE_TITLE = "如果今天第一次看这家公司"


def narrative(body: Mapping[str, Any]) -> dict[str, Any]:
    """The four answers, laid out as a document rather than as a payload."""

    answers = body["answers"]
    verdict = {"yes": "会建立观点", "no": "不会建立观点", "unclear": "说不好"}[
        answers["form_a_view"]
    ]
    sections = [
        {"heading": "一、今天第一次看，会不会建立观点",
         "body": f"{verdict}。{answers['because']}"},
        {"heading": "二、现有 thesis 哪几条会被重新写",
         "body": "\n".join(
             f"- {line['thesis_ref']}（{line['decision']}）：{line['line']}\n"
             f"  因为：{line['because']}"
             for line in answers["rewritten_lines"]
         ) or "没有一条会被重新写。"},
        {"heading": "三、哪些争论已经不重要了",
         "body": "\n".join(
             f"- {debate['debate_ref']}：{debate['because']}"
             for debate in answers["stale_debates"]
         ) or "现在开着的争论都还重要。"},
        {"heading": "四、下一个验证点与日期",
         "body": f"{answers['next_verification']['date']}："
                 f"{answers['next_verification']['what']}\n"
                 f"因为：{answers['next_verification']['because']}"},
    ]
    return {
        "title": NARRATIVE_TITLE,
        "subject_ref": body["company_ref"],
        "sections": sections,
        "summary": (
            f"{body['company_ref']}：{verdict}；"
            f"{len(answers['rewritten_lines'])} 条 thesis 会被重写，"
            f"{len(answers['stale_debates'])} 条争论已不重要；"
            f"下一个验证点 {answers['next_verification']['date']}。"
        ),
        "authority_note": (
            "这条记录只提案，不改任何权威：thesis 的改写是 ThesisRevisionCandidate，"
            "走 ADR-0007 的人裁决路径；debate 与 dossier 由它们自己的 lane 出新版本。"
        ),
    }


def build_review_body(
    context: Mapping[str, Any],
    answers: Mapping[str, Any],
    *,
    calibration_ref: str | None = None,
) -> dict[str, Any]:
    """Everything one review says, assembled.  Nothing written."""

    body = {
        "schema_version": SCHEMA_VERSION,
        "mission_ref": context["mission_ref"],
        "mission_version_ref": context["mission_version_ref"],
        "company_ref": context["company_ref"],
        "trigger": context["trigger"],
        "period_label": context["period_label"],
        "as_of": context["as_of"],
        "calibration_ref": calibration_ref,
        "inputs_hash": context["inputs_hash"],
        "questions": list(QUESTIONS),
        "answers": {
            key: value for key, value in answers.items()
            if key in OUTPUT_KEYS
        },
        "model": answers.get("model"),
        "outcome_counts": dict(context.get("outcome_counts") or {}),
    }
    body["narrative"] = narrative(body)
    return body


# ---------------------------------------------------------------------------
# the authority
# ---------------------------------------------------------------------------


def _decode(row: sqlite3.Row, name: str) -> dict[str, Any]:
    record = json.loads(row["record_json"])
    if record.get("content_hash") != row["content_hash"]:
        raise ZeroBaseReviewConflict(f"{name} did not read back as written")
    return record


class ZeroBaseReviewAuthority:
    """Append-only reviews, one version chain per company, plus its candidates.

    The only writer in this module.  It opens no thesis, no dossier, no debate
    map and no deliverable: everything it wants changed leaves here as a
    proposal for a person, which is the whole of ADR-0007 read from the other
    end.
    """

    def __init__(self, store: Any, *, clock: Callable[[], str] | None = None) -> None:
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("ZeroBaseReviewAuthority requires a DaltonStore")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        self.clock = clock or _now

    # -- reading -----------------------------------------------------------

    def latest(self, review_ref: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT v.record_json AS record_json, v.content_hash AS content_hash "
            "FROM zero_base_review_pointer p "
            "JOIN zero_base_review_versions v ON v.version_id = p.version_id "
            "WHERE p.review_ref = ?", (_text(review_ref, "review_ref"),),
        ).fetchone()
        return None if row is None else _decode(row, "ZeroBaseReview")

    def versions(self, review_ref: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT record_json, content_hash FROM zero_base_review_versions "
            "WHERE review_ref=? ORDER BY version_number",
            (_text(review_ref, "review_ref"),),
        ).fetchall()
        return [_decode(row, "ZeroBaseReview") for row in rows]

    def for_company(self, mission_ref: str, company_ref: str) -> dict[str, Any] | None:
        return self.latest(review_ref_for(mission_ref, company_ref))

    def candidates(self, company_ref: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT record_json, content_hash FROM zero_base_revision_candidates"
        params: list[Any] = []
        if company_ref is not None:
            query += " WHERE company_ref=?"
            params.append(_text(company_ref, "company_ref"))
        query += " ORDER BY created_at, candidate_id"
        return [
            _decode(row, "ZeroBaseRevisionCandidate")
            for row in self.connection.execute(query, params).fetchall()
        ]

    # -- writing -----------------------------------------------------------

    def record(self, body: Mapping[str, Any], *, actor_ref: str) -> dict[str, Any]:
        """Write one review, or return the identical one already written."""

        actor_ref = _text(actor_ref, "actor_ref", maximum=256)
        if not (actor_ref.startswith("automation:") or actor_ref.startswith("human:")):
            raise ZeroBaseReviewValidationError(
                "actor_ref must be an automation: or human: principal"
            )
        mission_ref = _text(body.get("mission_ref"), "mission_ref")
        mission_version_ref = _text(body.get("mission_version_ref"), "mission_version_ref")
        company_ref = _text(body.get("company_ref"), "company_ref")
        trigger = body.get("trigger")
        if trigger not in TRIGGERS:
            raise ZeroBaseReviewValidationError(f"trigger is one of {list(TRIGGERS)}")
        period_label = _text(body.get("period_label"), "period_label", maximum=256)
        if trigger == "monthly" and not _MONTH_RE.fullmatch(period_label):
            raise ZeroBaseReviewValidationError(
                "a monthly review's period_label is YYYY-MM"
            )
        digest = _text(body.get("inputs_hash"), "inputs_hash", maximum=128)
        answers = body.get("answers")
        if not isinstance(answers, Mapping) or set(answers) != OUTPUT_KEYS:
            raise ZeroBaseReviewValidationError(
                "a review carries exactly the four answers it was asked for"
            )
        if answers["form_a_view"] not in FORM_A_VIEW:
            raise ZeroBaseReviewValidationError(
                f"form_a_view is one of {list(FORM_A_VIEW)}"
            )
        ref = review_ref_for(mission_ref, company_ref)
        record = {
            **{key: value for key, value in body.items() if key != "content_hash"},
            "review_ref": ref,
            "actor_ref": actor_ref,
            "created_at": self.clock(),
        }
        with self.store._transaction() as cur:
            seen = cur.execute(
                "SELECT record_json, content_hash FROM zero_base_review_versions "
                "WHERE review_ref=? AND inputs_hash=?", (ref, digest),
            ).fetchone()
            if seen is not None:
                return {**_decode(seen, "ZeroBaseReview"), "status": "duplicate"}
            pointer = cur.execute(
                "SELECT version_id, version_number FROM zero_base_review_pointer "
                "WHERE review_ref=?", (ref,),
            ).fetchone()
            version = 1 if pointer is None else int(pointer["version_number"]) + 1
            prior = None if pointer is None else pointer["version_id"]
            record["version"] = version
            record["prior_version_ref"] = prior
            record["id"] = (
                "zero-base-review-version:"
                + content_hash({"ref": ref, "version": version})[:32]
            )
            record["content_hash"] = content_hash(
                {key: value for key, value in record.items() if key != "content_hash"}
            )
            cur.execute(
                "INSERT INTO zero_base_review_versions(version_id,review_ref,"
                "version_number,prior_version_ref,mission_ref,mission_version_ref,"
                "company_ref,trigger,period_label,form_a_view,rewritten_line_count,"
                "stale_debate_count,candidate_count,next_verification_date,inputs_hash,"
                "record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record["id"], ref, version, prior, mission_ref, mission_version_ref,
                 company_ref, trigger, period_label, answers["form_a_view"],
                 len(answers["rewritten_lines"]), len(answers["stale_debates"]),
                 # Candidates are written after the review exists -- they point
                 # at its version -- so the count here is what the review
                 # *proposes*, which is the same number when the mission grants
                 # the scope and a truthful zero when it does not.
                 len(answers["rewritten_lines"]),
                 answers["next_verification"]["date"], digest,
                 canonical_json(record), record["content_hash"], actor_ref,
                 record["created_at"]),
            )
            if pointer is None:
                cur.execute(
                    "INSERT INTO zero_base_review_pointer(review_ref,version_id,"
                    "version_number,company_ref,content_hash,updated_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (ref, record["id"], version, company_ref, record["content_hash"],
                     record["created_at"]),
                )
            else:
                cur.execute(
                    "UPDATE zero_base_review_pointer SET version_id=?, version_number=?, "
                    "content_hash=?, updated_at=? WHERE review_ref=?",
                    (record["id"], version, record["content_hash"], record["created_at"],
                     ref),
                )
        written = self.latest(ref)
        if written is None or written["id"] != record["id"]:
            raise ZeroBaseReviewConflict("the zero-base review did not read back")
        return {**written, "status": "fresh"}

    def record_revision_candidate(
        self,
        *,
        review: Mapping[str, Any],
        thesis: Mapping[str, Any],
        line: Mapping[str, Any],
        mission: Mapping[str, Any],
        actor_ref: str,
    ) -> dict[str, Any]:
        """ADR-0007's shape, raised by a review instead of by a judgement.

        The record is the one ``ThesisRevisionAuthority`` reads and decides on;
        what differs from the judgement lane's is the provenance -- a review
        version instead of a judgement -- and that is exactly what the person
        deciding needs to see, because "nothing happened and we would still
        write it differently" is a different argument from "this filing said
        something new".
        """

        actor_ref = _text(actor_ref, "actor_ref", maximum=256)
        decision = line.get("decision")
        if decision not in REWRITE_DECISIONS:
            raise ZeroBaseReviewValidationError(
                f"a candidate carries one of {list(REWRITE_DECISIONS)}"
            )
        refs = [_text(ref, "evidence_refs[]") for ref in (line.get("refs") or ())]
        if not refs:
            raise ZeroBaseReviewValidationError(
                "a thesis revision candidate must name the evidence that occasioned it"
            )
        if CANDIDATE_SCOPE not in mission["autonomy"]["may_write"]:
            raise ZeroBaseReviewValidationError(
                f"the mission does not grant {CANDIDATE_SCOPE}"
            )
        if CHECKPOINT_KIND not in mission["autonomy"]["human_checkpoints"]:
            raise ZeroBaseReviewValidationError(
                "a mission that grants the scope must carry the checkpoint"
            )
        record = {
            "schema_version": SCHEMA_VERSION,
            "id": "thesis-revision-candidate:" + content_hash({
                "review_version_ref": review["id"], "thesis_version_ref": thesis["ref"],
            })[:32],
            "created_at": self.clock(),
            "origin": "zero_base_review",
            "review_ref": review["review_ref"],
            "review_version_ref": review["id"],
            "judgement_ref": None,
            "thesis_version_ref": thesis["ref"],
            "thesis_version_hash": thesis["content_hash"],
            "thesis_ref": thesis.get("thesis_ref"),
            "company_ref": review["company_ref"],
            "decision": decision,
            "proposed_statement": _text(line["line"], "line", maximum=MAX_LINE_CHARS),
            "proposed_confidence": None,
            "falsifier_ref": None,
            "reflection_ref": None,
            "because": _text(line["because"], "because", maximum=MAX_BECAUSE_CHARS),
            "evidence_refs": list(dict.fromkeys(refs)),
            "checkpoint_kind": CHECKPOINT_KIND,
            "mission_version_ref": mission["id"],
            "mission_version_hash": mission["content_hash"],
            "actor_ref": actor_ref,
        }
        record["content_hash"] = content_hash(record)
        with self.store._transaction() as cur:
            existing = cur.execute(
                "SELECT record_json, content_hash FROM zero_base_revision_candidates "
                "WHERE candidate_id=?", (record["id"],),
            ).fetchone()
            if existing is not None:
                return {**_decode(existing, "ZeroBaseRevisionCandidate"),
                        "status": "duplicate"}
            cur.execute(
                "INSERT INTO zero_base_revision_candidates(candidate_id,review_ref,"
                "review_version_ref,thesis_version_ref,thesis_version_hash,company_ref,"
                "decision,checkpoint_kind,record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (record["id"], review["review_ref"], review["id"], thesis["ref"],
                 thesis["content_hash"], review["company_ref"], decision,
                 CHECKPOINT_KIND, canonical_json(record), record["content_hash"],
                 actor_ref, record["created_at"]),
            )
        return {**record, "status": "fresh"}


__all__ = [
    "CANDIDATE_SCOPE",
    "CHECKPOINT_KIND",
    "FORM_A_VIEW",
    "MAX_REWRITTEN_LINES",
    "MAX_STALE_DEBATES",
    "NARRATIVE_TITLE",
    "OUTPUT_KEYS",
    "PURPOSE",
    "VERIFIER_PURPOSE",
    "QUESTIONS",
    "REWRITE_DECISIONS",
    "SCHEMA_VERSION",
    "TRIGGERS",
    "WRITE_SCOPE",
    "ZeroBaseReviewAuthority",
    "ZeroBaseReviewConflict",
    "ZeroBaseReviewError",
    "ZeroBaseReviewValidationError",
    "allowed_refs",
    "build_context",
    "build_review_body",
    "build_review_prompt",
    "build_verifier_prompt",
    "company_debates",
    "due_reviews",
    "inputs_hash",
    "latest_calibration",
    "month_label",
    "narrative",
    "recent_events",
    "review",
    "review_ref_for",
    "review_state",
    "validate_review_output",
    "verify_review",
]
